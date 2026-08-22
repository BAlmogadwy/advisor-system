"""A truthful graduation what-if comparison must be ANSWERABLE.

A live 2026-08-22 turn asked «لو حذفت مقرر CS424 هل يؤثر على تخرجي؟» and
abstained twice over: first the scenario extractor missed the conditional
past tense (fixed in student_advisor_v2), and then the checker refused the
deterministic safe composer's OWN truthful comparison - the what-if payload
is the scenario report at top level, the baseline it was compared against
lives only under ``what_if.baseline``, and neither the figure miner nor the
metric binder read it, so «1 قبل التغيير، مقابل 2 بعده» was measured against
the scenario side alone.

Real executor -> real composer -> real checker, like the build-answer suite:
this defect class is a checker written to an imagined payload shape.
"""

from __future__ import annotations

import pytest

from core.models import (
    Course,
    Prerequisite,
    ProgrammeRequirement,
    Student,
    StudentCourse,
    StudentTermSection,
    TermSection,
)
from core.services.answer_consistency import check_answer
from core.services.rbac import ROLE_STUDENT
from core.services.student_advisor_v2 import _safe_graduation_answer
from core.services.virtual_advisor_capabilities import get_default_registry

pytestmark = pytest.mark.django_db

SID = 4_401_778
YEAR, TERM = 1448, 1

_CATALOGUE = frozenset({"TG101", "TG102", "TG103"})


@pytest.fixture
def chained_plan() -> None:
    """TG101 passed; TG102 registered this term; TG103 needs TG102.

    Dropping TG102 pushes it AND its dependant into future terms, so the
    baseline and the scenario genuinely disagree on every term estimate -
    the shape whose truthful comparison the checker refused.
    """
    student = Student.objects.create(
        student_id=SID,
        registration_no=str(SID),
        name="What-if student",
        program="TG",
        section="M",
        status="active",
    )
    courses = {}
    for code, programme_term in (("TG101", 1), ("TG102", 2), ("TG103", 3)):
        courses[code] = Course.objects.create(
            course_code=code, description=f"Course {code}", credit_hours=3
        )
        ProgrammeRequirement.objects.create(
            program="TG",
            course_code=code,
            course_name=f"Course {code}",
            type="Mandatory",
            programme_term=programme_term,
            credit_hours=3,
        )
    Prerequisite.objects.create(program="TG", course_code="TG103", prerequisite_course_code="TG102")
    StudentCourse.objects.create(student=student, course=courses["TG101"], status="passed")
    section = TermSection.objects.create(
        course_code="TG102",
        course_number="TG102",
        course_key="TG102",
        course_name="Course TG102",
        section="M1",
        available_capacity=30,
        registered_count=10,
    )
    StudentTermSection.objects.create(
        student_id=SID,
        academic_year=str(YEAR),
        term=str(TERM),
        term_section=section,
        source="scraper_timetable",
    )


def _what_if_payload(kind: str | None = "registered_timetable") -> dict:
    args: dict = {"remove_current_courses": ["TG102"]}
    if kind is not None:
        args["planning_baseline_kind"] = kind
    return get_default_registry().execute(
        "graduation_progress",
        args,
        scope={"role": ROLE_STUDENT, "student_id": SID},
        ctx={"academic_year": YEAR, "term": TERM},
    )


def _check(answer: str, payload: dict) -> list[str]:
    return check_answer(
        answer,
        tool_results=[payload],
        question="لو حذفت مقرر TG102 هل يؤثر على تخرجي؟",
        required_tools={"graduation_progress"},
        known_course_codes=_CATALOGUE,
    )


def test_identical_baselines_attach_no_alternate(chained_plan: None) -> None:
    """When the registered set IS the recommended set, a second simulation
    would restate the first - the executor attaches nothing."""
    payload = _what_if_payload()
    assert payload.get("what_if_alternate_baseline") is None


@pytest.fixture
def divergent_plan() -> None:
    """Registered {TG102}; the recommender would pick {TG102, TG104} - the
    owner's two-answer shape.  The recommender only offers courses whose
    declared plan-term PARITY matches the calendar term (1448/1 is odd), so
    the due courses are declared in odd terms here."""
    student = Student.objects.create(
        student_id=SID,
        registration_no=str(SID),
        name="What-if student",
        program="TG",
        section="M",
        status="active",
    )
    courses = {}
    for code, programme_term in (("TG101", 1), ("TG102", 1), ("TG104", 1), ("TG103", 2)):
        courses[code] = Course.objects.create(
            course_code=code, description=f"Course {code}", credit_hours=3
        )
        ProgrammeRequirement.objects.create(
            program="TG",
            course_code=code,
            course_name=f"Course {code}",
            type="Mandatory",
            programme_term=programme_term,
            credit_hours=3,
        )
    Prerequisite.objects.create(program="TG", course_code="TG103", prerequisite_course_code="TG102")
    StudentCourse.objects.create(student=student, course=courses["TG101"], status="passed")
    section = TermSection.objects.create(
        course_code="TG102",
        course_number="TG102",
        course_key="TG102",
        course_name="Course TG102",
        section="M1",
        available_capacity=30,
        registered_count=10,
    )
    StudentTermSection.objects.create(
        student_id=SID,
        academic_year=str(YEAR),
        term=str(TERM),
        term_section=section,
        source="scraper_timetable",
    )


def test_divergent_baselines_run_the_same_change_on_both(divergent_plan: None) -> None:
    """The owner's rule: same function, the other parameter.  The alternate
    carries the OTHER baseline kind, a valid what-if of the SAME change, and
    the course that makes the baselines differ."""
    payload = _what_if_payload()
    alternate = payload.get("what_if_alternate_baseline")

    assert isinstance(alternate, dict)
    assert alternate["planning_baseline_kind"] == "recommended_current_term"
    alt_what_if = alternate["what_if"]
    assert alt_what_if["valid"] is True
    alt_codes = {
        row["code"] for row in alt_what_if["baseline"]["planning_baseline_courses_assumed_passed"]
    }
    primary_codes = {
        row["code"]
        for row in payload["what_if"]["baseline"]["planning_baseline_courses_assumed_passed"]
    }
    assert "TG104" in alt_codes - primary_codes


def test_the_composed_answer_carries_both_baselines_and_survives_the_checker(
    divergent_plan: None,
) -> None:
    payload = _what_if_payload()
    answer = _safe_graduation_answer("Arabic", [payload], "")

    assert "المقارنة نفسها وفق" in answer
    assert "TG104" in answer
    assert "التقدير الكامل" in answer
    assert _check(answer, payload) == []


def test_the_answer_opens_with_a_verdict_and_scopes_the_effect_line(
    divergent_plan: None,
) -> None:
    """The owner's live read of the first shipped shape: an unscoped «لم
    يتغيّر…» at the top followed by a section whose numbers change read as a
    wrong sentence.  The answer now OPENS with a verdict naming each
    baseline beside its own outcome, and the effect line carries its
    baseline's name."""
    payload = _what_if_payload()
    answer = _safe_graduation_answer("Arabic", [payload], "")

    first_line = answer.splitlines()[0]
    assert first_line.startswith("الخلاصة:")
    assert "الجدول المسجّل فعليًا" in first_line
    assert "الموصى بها" in first_line
    assert "\nوفق الجدول المسجّل فعليًا: " in answer
    assert "وبما أن مقررات البداية أعلاه تختلف عن" in answer
    assert _check(answer, payload) == []


def test_the_verdict_phrase_tracks_the_computed_effect() -> None:
    from core.services.student_advisor_v2 import _what_if_verdict_phrase

    assert (
        _what_if_verdict_phrase({"timing_effect": "SAME"}, "Arabic") == "لا يغيّر تقدير موعد تخرجك"
    )
    later = _what_if_verdict_phrase({"timing_effect": "LATER", "term_difference": 2}, "Arabic")
    assert "يؤخّر" in later and "2" in later
    earlier = _what_if_verdict_phrase({"timing_effect": "EARLIER", "terms_saved": 1}, "English")
    assert "forward" in earlier and "1 term" in earlier
    assert _what_if_verdict_phrase({"timing_effect": "NOT_DETERMINABLE"}, "Arabic") == ""
    assert _what_if_verdict_phrase({"timing_effect": "UNRESOLVED_WORSE"}, "English") == ""


def test_the_default_orientation_names_the_course_the_registered_side_lacks(
    divergent_plan: None,
) -> None:
    """The review's blocking find: in the DEFAULT orientation the primary is
    the recommended baseline - the one WITH the extra course - and the
    one-directional set was empty, so the live answer showed two disagreeing
    numbers and never said why.  The clause is symmetric now: the registered
    alternate is described as EXCLUDING the course."""
    payload = _what_if_payload(kind=None)
    alternate = payload.get("what_if_alternate_baseline")

    assert isinstance(alternate, dict)
    assert alternate["planning_baseline_kind"] == "registered_timetable"
    answer = _safe_graduation_answer("Arabic", [payload], "")
    assert "ولا تشمل TG104" in answer
    assert _check(answer, payload) == []
    english = _safe_graduation_answer("English", [payload], "")
    assert "which excludes TG104" in english


@pytest.fixture
def matched_plan() -> None:
    """Registered {TG102} and recommended {TG102} - the same set, and the
    change VALIDATES on both sides, so only the differ guard can suppress
    the alternate."""
    student = Student.objects.create(
        student_id=SID,
        registration_no=str(SID),
        name="What-if student",
        program="TG",
        section="M",
        status="active",
    )
    courses = {}
    for code, programme_term in (("TG101", 1), ("TG102", 1), ("TG103", 2)):
        courses[code] = Course.objects.create(
            course_code=code, description=f"Course {code}", credit_hours=3
        )
        ProgrammeRequirement.objects.create(
            program="TG",
            course_code=code,
            course_name=f"Course {code}",
            type="Mandatory",
            programme_term=programme_term,
            credit_hours=3,
        )
    Prerequisite.objects.create(program="TG", course_code="TG103", prerequisite_course_code="TG102")
    StudentCourse.objects.create(student=student, course=courses["TG101"], status="passed")
    section = TermSection.objects.create(
        course_code="TG102",
        course_number="TG102",
        course_key="TG102",
        course_name="Course TG102",
        section="M1",
        available_capacity=30,
        registered_count=10,
    )
    StudentTermSection.objects.create(
        student_id=SID,
        academic_year=str(YEAR),
        term=str(TERM),
        term_section=section,
        source="scraper_timetable",
    )


def test_matching_baselines_suppress_the_alternate_via_the_differ_guard(
    matched_plan: None,
) -> None:
    payload = _what_if_payload()
    alt_probe = get_default_registry().execute(
        "graduation_progress",
        {
            "remove_current_courses": ["TG102"],
            "planning_baseline_kind": "recommended_current_term",
        },
        scope={"role": ROLE_STUDENT, "student_id": SID},
        ctx={"academic_year": YEAR, "term": TERM},
    )
    # The suppression must come from the DIFFER guard, not from validity.
    assert alt_probe["what_if"]["valid"] is True
    assert payload.get("what_if_alternate_baseline") is None


def test_an_alternate_failure_never_kills_the_primary_answer(
    divergent_plan: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The alternate is an enhancement.  Un-isolated, an exception in it
    rode up to the registry's catch-all and turned the WHOLE call into
    ok:False - the student lost the primary simulation they were owed and
    the turn could only refuse."""
    from core.services import student_graduation as sg

    real = sg.build_graduation_what_if
    calls = {"n": 0}

    def exploding_second(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] > 1:
            raise RuntimeError("production-data edge in the alternate baseline")
        return real(*args, **kwargs)

    monkeypatch.setattr(sg, "build_graduation_what_if", exploding_second)
    payload = _what_if_payload()

    assert payload.get("ok", True) is True
    assert isinstance(payload.get("what_if"), dict)
    assert payload["what_if"]["valid"] is True
    assert payload.get("what_if_alternate_baseline") is None


def test_a_malformed_alternate_payload_never_kills_the_primary_answer(
    divergent_plan: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Path (b) from the approval: the try must cover the whole alternate
    PATH, not just the call - a malformed second payload used to raise
    inside the differ comparison, three lines after the guarded call, and
    ride to the same catch-all."""
    from core.services import student_graduation as sg

    real = sg.build_graduation_what_if
    calls = {"n": 0}

    def malformed_second(*args, **kwargs):
        calls["n"] += 1
        result = real(*args, **kwargs)
        if calls["n"] > 1 and isinstance(result.get("what_if"), dict):
            result["what_if"]["baseline"]["planning_baseline_courses_assumed_passed"] = 42
        return result

    monkeypatch.setattr(sg, "build_graduation_what_if", malformed_second)
    payload = _what_if_payload()

    assert payload.get("ok", True) is True
    assert isinstance(payload.get("what_if"), dict)
    assert payload["what_if"]["valid"] is True


def test_a_composer_alternate_failure_costs_only_the_second_section() -> None:
    """The symmetric guard in the last-line-of-defence composer: a malformed
    alternate loses its section, never the whole safe answer."""
    from core.services.student_advisor_v2 import _safe_graduation_what_if_answer_base

    what_if = {
        "valid": True,
        "removed_current_courses": [{"code": "TG102"}],
        "added_current_courses": [],
        "comparison": {},
        "baseline": {"lower_bound_additional_terms": 1},
        "scenario": {"lower_bound_additional_terms": 2},
    }
    malformed_alternate = {
        "planning_baseline_kind": "recommended_current_term",
        "what_if": {
            "valid": True,
            "baseline": {"planning_baseline_courses_assumed_passed": 42},
            "scenario": {},
        },
    }
    answer = _safe_graduation_what_if_answer_base(
        "Arabic", what_if, "registered_timetable", malformed_alternate
    )
    assert "TG102" in answer
    assert "الحد الأدنى" in answer


def test_a_producer_without_display_rows_cannot_disable_the_feature(
    divergent_plan: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The review's fragility find: two ABSENT course lists compared equal
    and silently suppressed the alternate.  With the rows stripped, the
    credit totals still establish the difference."""
    from core.services import student_graduation as sg

    real = sg.build_graduation_what_if

    def stripped(*args, **kwargs):
        result = real(*args, **kwargs)
        what_if = result.get("what_if") if isinstance(result, dict) else None
        if isinstance(what_if, dict):
            for side in ("baseline", "scenario"):
                summary = what_if.get(side)
                if isinstance(summary, dict):
                    summary.pop("planning_baseline_courses_assumed_passed", None)
        return result

    monkeypatch.setattr(sg, "build_graduation_what_if", stripped)
    payload = _what_if_payload()
    assert isinstance(payload.get("what_if_alternate_baseline"), dict)


def test_the_differing_courses_clause_caps_at_three() -> None:
    from core.services.student_advisor_v2 import _what_if_alternate_lines

    alternate = {
        "planning_baseline_kind": "recommended_current_term",
        "what_if": {
            "valid": True,
            "baseline": {
                "lower_bound_additional_terms": 1,
                "planning_baseline_courses_assumed_passed": [{"code": f"TG9{i}"} for i in range(5)],
            },
            "scenario": {"lower_bound_additional_terms": 2},
        },
    }
    lines = _what_if_alternate_lines("Arabic", alternate, {"baseline": {}})
    heading = lines[0]
    assert sum(1 for i in range(5) if f"TG9{i}" in heading) == 3


def test_the_primary_section_keeps_its_own_full_estimate_line(divergent_plan: None) -> None:
    """The owner's presentation fix is deletable-free no more: the string
    appears once in the PRIMARY section and once in the alternate, so a
    composer that drops the primary line fails the count."""
    payload = _what_if_payload()
    arabic = _safe_graduation_answer("Arabic", [payload], "")
    assert arabic.count("التقدير الكامل") == 2
    english = _safe_graduation_answer("English", [payload], "")
    assert english.count("Full estimate:") == 2


def test_prose_codes_fold_and_the_unminable_are_dropped() -> None:
    """The reviewer's R1/R2: «AI٤٩١» must fold to AI491 (not print a bare
    AI), and a code whose cleaned spelling the answer miner cannot see as a
    course token must be dropped rather than interpolated - printed, it
    reads as a fabrication or makes the safe answer refuse itself."""
    from core.services.student_advisor_v2 import _what_if_alternate_lines

    alternate = {
        "planning_baseline_kind": "recommended_current_term",
        "what_if": {
            "valid": True,
            "baseline": {
                "lower_bound_additional_terms": 1,
                "planning_baseline_courses_assumed_passed": [
                    {"code": "AI٤٩١"},
                    {"code": "ZZ 101"},
                    {"code": "<b>QQ202</b>"},
                ],
            },
            "scenario": {"lower_bound_additional_terms": 2},
        },
    }
    heading = _what_if_alternate_lines("Arabic", alternate, {"baseline": {}})[0]
    assert "AI491" in heading
    assert "ZZ101" in heading
    assert "QQ202" not in heading and "BQQ202B" not in heading


def test_a_single_additional_term_reads_singular_in_english() -> None:
    from core.services.student_advisor_v2 import _safe_graduation_what_if_answer_base

    what_if = {
        "valid": True,
        "removed_current_courses": [{"code": "TG102"}],
        "added_current_courses": [],
        "comparison": {},
        "baseline": {"lower_bound_additional_terms": 1, "estimated_additional_terms": 1},
        "scenario": {"lower_bound_additional_terms": 2, "estimated_additional_terms": 2},
    }
    answer = _safe_graduation_what_if_answer_base("English", what_if, "registered_timetable")
    assert "1 additional term before" in answer
    assert "1 additional terms" not in answer


def test_a_missing_lower_bound_never_prints_the_word_none() -> None:
    from core.services.student_advisor_v2 import _safe_graduation_what_if_answer_base

    what_if = {
        "valid": True,
        "removed_current_courses": [{"code": "TG102"}],
        "added_current_courses": [],
        "comparison": {},
        "baseline": {"lower_bound_additional_terms": None},
        "scenario": {"lower_bound_additional_terms": 2},
    }
    for language in ("Arabic", "English"):
        answer = _safe_graduation_what_if_answer_base(language, what_if, "registered_timetable")
        assert "None" not in answer, language


def test_a_wrong_figure_in_the_alternate_section_still_flags(divergent_plan: None) -> None:
    """Two-sided: admitting the second baseline's figures must not admit
    inventions inside its section."""
    payload = _what_if_payload()
    answer = _safe_graduation_answer("Arabic", [payload], "")
    alt_before = payload["what_if_alternate_baseline"]["what_if"]["baseline"][
        "lower_bound_additional_terms"
    ]
    marker = f"{alt_before} قبل التغيير"
    assert answer.count(marker) >= 1
    wrong = answer.replace(marker, "9 قبل التغيير", 1)
    if wrong == answer:
        pytest.fail("the alternate section must state its baseline lower bound")
    assert _check(wrong, payload) != []


def test_alternate_baseline_figures_are_supported_on_their_own() -> None:
    """Checker-side: the alternate's sides are admissible values of the same
    metric.  1 and 2 appear NOWHERE in the primary pair (5, 5), so only the
    alternate read supports the truthful sentence."""
    payload = {
        "tool": "graduation_progress",
        "ok": True,
        "simulation_completed": True,
        "lower_bound_additional_terms": 5,
        "what_if": {
            "valid": True,
            "baseline": {"lower_bound_additional_terms": 5},
            "scenario": {"lower_bound_additional_terms": 5},
        },
        "what_if_alternate_baseline": {
            "planning_baseline_kind": "recommended_current_term",
            "what_if": {
                "valid": True,
                "baseline": {"lower_bound_additional_terms": 1},
                "scenario": {"lower_bound_additional_terms": 2},
            },
        },
    }

    def run(answer: str) -> list[str]:
        return check_answer(
            answer,
            tool_results=[payload],
            question="لو حذفت مقرر TG102 هل يؤثر على تخرجي؟",
            required_tools=set(),
            known_course_codes=_CATALOGUE,
        )

    truthful = "وفق المقررات الموصى بها، الحد الأدنى للفصول الإضافية 1 قبل التغيير مقابل 2 بعده."
    invented = "وفق المقررات الموصى بها، الحد الأدنى للفصول الإضافية 3 قبل التغيير مقابل 7 بعده."
    assert run(truthful) == []
    assert run(invented) != []


def test_the_projection_carries_the_alternate_baseline(divergent_plan: None) -> None:
    from core.services.llm_remote_privacy import (
        RemoteIdentityMap,
        project_tool_result_for_remote,
    )

    payload = _what_if_payload()
    out = project_tool_result_for_remote("graduation_progress", payload, RemoteIdentityMap())

    alternate = out.get("what_if_alternate_baseline")
    assert isinstance(alternate, dict)
    assert alternate["planning_baseline_kind"] == "recommended_current_term"
    assert isinstance(alternate.get("what_if"), dict)


def test_the_scenario_really_disagrees_with_its_baseline(chained_plan: None) -> None:
    payload = _what_if_payload()
    what_if = payload["what_if"]

    assert what_if["valid"] is True
    baseline, scenario = what_if["baseline"], what_if["scenario"]
    # The composer's comparison line states exactly this pair - the
    # production shape («الحد الأدنى…: 1 قبل التغيير، مقابل 2 بعده»).
    assert baseline["lower_bound_additional_terms"] != scenario["lower_bound_additional_terms"], (
        "the fixture must produce a real before/after difference"
    )


def test_the_truthful_what_if_comparison_survives_the_checker(chained_plan: None) -> None:
    payload = _what_if_payload()
    answer = _safe_graduation_answer("Arabic", [payload], "")

    assert answer, "the safe composer must produce a comparison answer"
    assert _check(answer, payload) == []


def test_the_stated_difference_between_the_sides_is_a_supported_figure() -> None:
    """«بمقدار 3 فصول» where the sides are 1 and 4: the difference is a
    server-computed comparative fact and equals NEITHER side.  Checker-side
    arithmetic, so a synthetic payload is the right instrument here."""
    payload = {
        "tool": "graduation_progress",
        "ok": True,
        "simulation_completed": True,
        "lower_bound_additional_terms": 4,
        "what_if": {
            "valid": True,
            "baseline": {"lower_bound_additional_terms": 1},
            "scenario": {"lower_bound_additional_terms": 4},
        },
    }
    supported = "يرتفع الحد الأدنى للفصول الإضافية بمقدار 3 فصول بعد التغيير."
    unsupported = "يرتفع الحد الأدنى للفصول الإضافية بمقدار 5 فصول بعد التغيير."

    def run(answer: str) -> list[str]:
        return check_answer(
            answer,
            tool_results=[payload],
            question="لو حذفت مقرر TG102 هل يؤثر على تخرجي؟",
            required_tools=set(),
            known_course_codes=_CATALOGUE,
        )

    assert run(supported) == []
    assert run(unsupported) != []


def test_the_two_figure_lower_bound_sentence_binds_to_its_own_words() -> None:
    """The review's reproduction on a completed single report: «الحد
    الأدنى: 2 فصل إضافي، أي 3 فصول بما فيها الفصل الحالي» quoted both
    lower_bound_* values exactly and was refused, because the two-figure
    branch chose its fields from the completeness heuristic alone."""
    payload = {
        "tool": "graduation_progress",
        "ok": True,
        "simulation_completed": True,
        "estimated_additional_terms": 3,
        "lower_bound_additional_terms": 2,
        "estimated_terms_including_planning_baseline": 4,
        "lower_bound_terms_including_planning_baseline": 3,
    }

    def run(answer: str) -> list[str]:
        return check_answer(
            answer,
            tool_results=[payload],
            question="كم فصلاً يتبقى لتخرجي؟",
            required_tools=set(),
            known_course_codes=_CATALOGUE,
        )

    truthful_lower = "تحتاج على الأقل 2 فصل إضافي، أي 3 فصول بما فيها الفصل الحالي."
    truthful_estimate = "تحتاج 3 فصول إضافية، أي 4 فصول بما فيها الفصل الحالي."
    wrong_lower = "تحتاج على الأقل 3 فصول إضافية، أي 4 فصول بما فيها الفصل الحالي."
    assert run(truthful_lower) == []
    assert run(truthful_estimate) == []
    assert run(wrong_lower) != []


def test_english_lower_bound_words_bind_the_lower_bound_field() -> None:
    """«على الأقل» / "at least" / "minimum" are load-bearing entries of the
    lower-bound word list, not decoration around «الحد الأدنى»."""
    payload = {
        "tool": "graduation_progress",
        "ok": True,
        "simulation_completed": True,
        "estimated_additional_terms": 3,
        "lower_bound_additional_terms": 2,
    }

    def run(answer: str) -> list[str]:
        return check_answer(
            answer,
            tool_results=[payload],
            question="How many terms remain?",
            required_tools=set(),
            known_course_codes=_CATALOGUE,
        )

    assert run("You need at least 2 additional terms.") == []
    assert run("You need at least 5 additional terms.") != []


def test_a_baseline_side_value_is_supported_on_its_own() -> None:
    """The sides are load-bearing, not just their difference: before=2 with
    after=5 puts the difference at 3 and the top level at 5, so only the
    side read makes the truthful «كان الحد الأدنى 2 قبل التغيير» pass."""
    payload = {
        "tool": "graduation_progress",
        "ok": True,
        "simulation_completed": True,
        "lower_bound_additional_terms": 5,
        "what_if": {
            "valid": True,
            "baseline": {"lower_bound_additional_terms": 2},
            "scenario": {"lower_bound_additional_terms": 5},
        },
    }

    def run(answer: str) -> list[str]:
        return check_answer(
            answer,
            tool_results=[payload],
            question="لو حذفت مقرر TG102 هل يؤثر على تخرجي؟",
            required_tools=set(),
            known_course_codes=_CATALOGUE,
        )

    assert run("كان الحد الأدنى للفصول الإضافية 2 قبل التغيير.") == []
    assert run("كان الحد الأدنى للفصول الإضافية 4 قبل التغيير.") != []


def test_an_improvement_difference_is_supported_in_absolute_value() -> None:
    """A scenario that SAVES a term states the difference too — the sides
    are 3 then 2, the difference the answer speaks is 1, and only the
    absolute value supports it; a signed difference would refuse every
    improvement sentence."""
    payload = {
        "tool": "graduation_progress",
        "ok": True,
        "simulation_completed": True,
        "lower_bound_additional_terms": 2,
        "what_if": {
            "valid": True,
            "baseline": {"lower_bound_additional_terms": 3},
            "scenario": {"lower_bound_additional_terms": 2},
        },
    }

    def run(answer: str) -> list[str]:
        return check_answer(
            answer,
            tool_results=[payload],
            question="لو حذفت مقرر TG102 هل يؤثر على تخرجي؟",
            required_tools=set(),
            known_course_codes=_CATALOGUE,
        )

    assert run("ينخفض الحد الأدنى للفصول الإضافية بمقدار 1 فصل بعد التغيير.") == []
    assert run("ينخفض الحد الأدنى للفصول الإضافية بمقدار 4 فصول بعد التغيير.") != []


def test_a_wrong_before_figure_in_the_comparison_still_flags(chained_plan: None) -> None:
    """Two-sided: admitting the baseline's figures must not admit inventions."""
    payload = _what_if_payload()
    answer = _safe_graduation_answer("Arabic", [payload], "")
    before = payload["what_if"]["baseline"]["lower_bound_additional_terms"]
    wrong = answer.replace(f": {before} قبل التغيير", ": 9 قبل التغيير")

    assert wrong != answer, "the composed answer must state the baseline lower bound"
    assert _check(wrong, payload) != []
