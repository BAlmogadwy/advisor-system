"""Server-owned synthesis for multi-capability Student Advisor V2.1 answers.

Individual capability renderers intentionally describe only their own evidence.
Some questions, however, ask for a relationship between two verified result sets.
Those relationships are computed here rather than left to model prose.  Every
statement is a bounded set comparison over provider-visible course codes; it does
not infer eligibility, offering, seats, or an optimal graduation plan.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Any

CURRENT_TIMETABLE_PRIORITY_SCOPE = "current_timetable_priority_assessment"
TIMETABLE_BUILD_PRIORITY_SCOPE = "timetable_build_priority_assessment"

JOINED_SCOPE_TOOLS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        CURRENT_TIMETABLE_PRIORITY_SCOPE: ("my_timetable", "my_progress"),
        TIMETABLE_BUILD_PRIORITY_SCOPE: (
            "build_timetable_proposal",
            "my_progress",
        ),
    }
)


_AR_UNPLACED_REASONS: Mapping[str, str] = MappingProxyType(
    {
        "OMITTED_IN_THIS_VARIANT": (
            "لم يضع هذا الخيار المقرر، بينما وضعه خيار آخر مولّد؛ قارن بالخيارات الأخرى."
        ),
        "NOT_ON_FILE": (
            "لا توجد شعبة مسجلة لهذا المقرر في لقطة البيانات؛ وهذا لا يثبت أن الجامعة لا تطرحه."
        ),
        "ALL_SECTIONS_CLASH": "تتعارض كل الشُعب المسجلة مع مقرر آخر في هذا الخيار.",
        "DID_NOT_FIT": "لم يمكن وضع المقرر مع بقية هذا الخيار ضمن القيود المحددة.",
        "MEETING_DATA_INCOMPLETE": (
            "بيانات لقاءات إحدى الشُعب ناقصة أو غير صالحة، لذلك تعذر إثبات خلوها من التعارض."
        ),
        "PREREQUISITES": "المتطلبات السابقة المسجلة لهذا المقرر غير مستوفاة.",
        "CREDIT_LIMIT": "لم يتسع حد الساعات المحدد لهذا المقرر في هذا الخيار.",
        "PIN_CONFLICTS_WITH_RETAINED_SECTION": (
            "تتعارض الشعبة المثبتة مع شعبة محتفظ بها في خط الأساس."
        ),
        "PIN_NOT_IN_RETAINED_BASELINE": (
            "لا تطابق الشعبة المثبتة الشعبة المحتفظ بها في خط الأساس."
        ),
    }
)

_AR_CONSTRAINT_FAILURE_REASONS: Mapping[str, str] = MappingProxyType(
    {
        "NOT_ON_FILE": (
            "لا توجد شعبة مسجلة لهذا المقرر في لقطة البيانات؛ وهذا لا يثبت أن الجامعة لا تطرحه."
        ),
        "ALL_SECTIONS_CLASH": "تتعارض كل الشُعب المسجلة مع القيود الزمنية المثبتة.",
        "DID_NOT_FIT": "تعذر وضع المقرر ضمن القيود المحددة في نطاق فحص الجدولة.",
        "MEETING_DATA_INCOMPLETE": (
            "بيانات لقاءات إحدى الشُعب ناقصة أو غير صالحة، لذلك تعذر إثبات خلوها من التعارض."
        ),
        "PREREQUISITES": "المتطلبات السابقة المسجلة لهذا المقرر غير مستوفاة.",
        "CREDIT_LIMIT": "لم يتسع حد الساعات المحدد لهذا المقرر.",
        "PIN_CONFLICTS_WITH_RETAINED_SECTION": (
            "تتعارض الشعبة المثبتة مع شعبة محتفظ بها في خط الأساس."
        ),
        "PIN_NOT_IN_RETAINED_BASELINE": (
            "لا تطابق الشعبة المثبتة الشعبة المحتفظ بها في خط الأساس."
        ),
    }
)

_RETAINED_BASELINE_OVER_MAX_REASON = re.compile(
    r"retained baseline has\s+(?P<baseline>\d+)\s+credits?.*?"
    r"maximum of\s+(?P<maximum>\d+)",
    re.IGNORECASE,
)

_AR_TIMETABLE_DAYS: Mapping[str, str] = MappingProxyType(
    {
        "SUN": "الأحد",
        "MON": "الاثنين",
        "TUE": "الثلاثاء",
        "WED": "الأربعاء",
        "THU": "الخميس",
        "FRI": "الجمعة",
        "SAT": "السبت",
    }
)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _code(value: Any) -> str:
    return _text(value).upper()


def _mapping_rows(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _unique_codes(values: Sequence[Any]) -> list[str]:
    result: list[str] = []
    for value in values:
        code = _code(value)
        if code and code not in result:
            result.append(code)
    return result


def _code_values(value: Any) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        return []
    return _unique_codes(value)


def _row_codes(rows: Any, field: str) -> list[str]:
    return _unique_codes([row.get(field) for row in _mapping_rows(rows)])


def _registered_codes(timetable: Mapping[str, Any]) -> list[str]:
    registrations = _row_codes(timetable.get("registrations"), "course_code")
    if registrations:
        return registrations
    return _row_codes(timetable.get("meetings"), "course_code")


def _priority_codes(progress: Mapping[str, Any]) -> list[str]:
    return _row_codes(progress.get("unlock_impact_ranking"), "code")


def _proposal_codes(proposal: Mapping[str, Any]) -> list[str]:
    codes: list[str] = []
    for alternative in _mapping_rows(proposal.get("alternatives")):
        codes.extend(_row_codes(alternative.get("courses"), "course_code"))
    if not codes and proposal.get("no_additional_courses"):
        codes.extend(_row_codes(proposal.get("baseline_sections"), "course_code"))
    return _unique_codes(codes)


def _shown(values: Sequence[str], *, language: str) -> str:
    return ("، ".join(values) if language == "Arabic" else ", ".join(values)) or (
        "لا يوجد" if language == "Arabic" else "none"
    )


def localize_timetable_unplaced_reason(language: str, item: Mapping[str, Any]) -> str:
    """Return a deterministic locale-safe reason for one unplaced variant row.

    Planner explanations are English implementation text.  Arabic answers use the
    stable reason code and never echo an untranslated implementation fallback.
    Already-Arabic source text is retained for forward compatibility.
    """

    raw = _text(item.get("reason"))
    if language != "Arabic":
        return raw or "The course was not placed in this option under the specified constraints."
    if raw and any("\u0600" <= character <= "\u06ff" for character in raw):
        return raw
    reason_code = _code(item.get("reason_code"))
    return _AR_UNPLACED_REASONS.get(
        reason_code,
        "لم يُدرج المقرر في هذا الخيار ضمن القيود المحددة.",
    )


def localize_timetable_constraint_failure_reason(language: str, item: Mapping[str, Any]) -> str:
    """Keep executor diagnostics out of Arabic hard-constraint summaries.

    Constraint failures currently cross the remote boundary without a stable
    reason code, so translating individual English implementation messages would
    be brittle.  Preserve an already-Arabic explanation, retain the exact English
    explanation for English answers, and otherwise use one bounded Arabic reason.
    """

    raw = _text(item.get("reason"))
    if language != "Arabic":
        return raw or "This constraint could not be satisfied by the bounded timetable check."
    if raw and any("\u0600" <= character <= "\u06ff" for character in raw):
        return raw
    baseline_over_max = _RETAINED_BASELINE_OVER_MAX_REASON.search(raw)
    if baseline_over_max:
        return (
            f"يحمل خط الأساس المحتفظ به {baseline_over_max.group('baseline')} ساعة معتمدة، "
            f"وهي تتجاوز الحد الأعلى الفعّال البالغ {baseline_over_max.group('maximum')} "
            "ساعة في هذا الفحص."
        )
    return _AR_CONSTRAINT_FAILURE_REASONS.get(
        _code(item.get("reason_code")),
        "تعذر تحقيق هذا القيد ضمن نطاق فحص الجدولة.",
    )


def localize_timetable_day(language: str, value: Any) -> str:
    """Localize a stored weekday token for display without altering evidence."""

    day = _code(value)
    if language == "Arabic":
        return _AR_TIMETABLE_DAYS.get(day, _text(value))
    return _text(value)


def render_current_timetable_priority_assessment(
    language: str,
    timetable: Mapping[str, Any],
    progress: Mapping[str, Any],
) -> str:
    """Assess registered-plan alignment without claiming optimality or eligibility."""

    registered = _registered_codes(timetable)
    registered_set = set(registered)
    plan_current = _code_values(progress.get("registered_requirement_course_codes"))
    plan_current_set = set(plan_current)
    exact_plan_matches = [code for code in registered if code in plan_current_set]
    ranked = _priority_codes(progress)
    exact_priority_overlap = [code for code in ranked if code in registered_set]
    shown_priority_gaps = [code for code in ranked if code not in registered_set][:5]
    expected = (
        bool(timetable.get("is_expected_plan"))
        or _code(timetable.get("schedule_kind")) == "EXPECTED_PLAN"
    )

    if language == "Arabic":
        if expected:
            return (
                "الخلاصة المحدودة: السجل المعروض جدول متوقع للتخطيط وليس تسجيلًا فعليًا؛ "
                "لذلك لا يمكن الحكم منه على المواد التي سجلتها فعليًا."
            )
        if not registered:
            return (
                "الخلاصة المحدودة: لا يحتوي دليل الجدول المعروض على مقررات مسجلة يمكن "
                "مقارنتها بترتيب الأولوية، لذلك لا يمكن وصف اختيارات هذا الفصل بأنها صحيحة "
                "أو غير صحيحة."
            )

        if len(exact_plan_matches) == len(registered):
            conclusion = (
                "الخلاصة المحدودة: تتسق كل رموز مقررات جدولك المسجّل مع هويات متطلبات "
                "يعاملها سجل التقدم على أنها قيد الدراسة حاليًا. هذا يثبت اتساق السجلين، "
                "لكنه لا يثبت أن الجدول هو الاختيار الأكاديمي الأفضل أو الأسرع للتخرج."
            )
        elif exact_plan_matches:
            conclusion = (
                "الخلاصة المحدودة: يتسق الجدول جزئيًا مع سجل التقدم بالمطابقة الحرفية "
                "للرموز. لا تسمح هذه الأدلة وحدها بالحكم بأن الرموز الأخرى خاطئة؛ فقد تكون "
                "لها هوية اختيارية أو بديلة لا تظهر في هذا العرض."
            )
        else:
            conclusion = (
                "الخلاصة المحدودة: لا يظهر تطابق حرفي بين رموز الجدول وهويات المتطلبات "
                "المسجلة في عرض التقدم. لا يثبت ذلك أن الاختيارات خاطئة، لأن هذا العرض لا "
                "يكشف كل علاقات المقررات الاختيارية والرموز البديلة."
            )

        lines = [
            conclusion,
            (
                "التطابق الحرفي بين الجدول المسجّل وهويات المتطلبات المسجلة حاليًا: "
                f"{len(exact_plan_matches)} من {len(registered)} — "
                f"{_shown(exact_plan_matches, language=language)}."
            ),
            (
                "قائمة الأولوية المتبقية التالية ليست توصية تسجيل، بل مقارنة مستقلة "
                "وفق أثر فتح سلاسل المتطلبات."
            ),
            (
                "التداخل الحرفي مع قائمة أولوية المقررات المتبقية المعروضة: "
                f"{_shown(exact_priority_overlap, language=language)}."
            ),
        ]
        if not exact_priority_overlap and ranked:
            lines.append(
                "عدم التداخل متوقع هنا؛ قائمة الأولوية تخص المقررات المتبقية المستوفية "
                "للمتطلبات، ولا تشمل ما يعامله سجل التقدم على أنه قيد الدراسة. لذلك لا "
                "يعني عدم التداخل أن جدولك خاطئ."
            )
        if shown_priority_gaps:
            lines.append(
                "أعلى فجوات الأولوية المعروضة خارج الجدول الحالي: "
                f"{_shown(shown_priority_gaps, language=language)}."
            )
        lines.append(
            "هذه الفجوات مرتبة حسب أثر فتح سلاسل المتطلبات فقط، وليست توصية تسجيل: لا "
            "تثبت طرح شعبة أو وجود مقعد أو السماح بالتسجيل، ولا تقارن بديلًا كاملًا للجدول."
        )
        return "\n".join(lines)

    if expected:
        return (
            "Bounded conclusion: the displayed record is an expected planning timetable, "
            "not actual registration, so it cannot assess which courses you actually registered."
        )
    if not registered:
        return (
            "Bounded conclusion: the displayed timetable evidence contains no registered "
            "course rows to compare with the priority ranking, so it cannot label this term's "
            "choices right or wrong."
        )

    if len(exact_plan_matches) == len(registered):
        conclusion = (
            "Bounded conclusion: every registered timetable code aligns with a requirement "
            "identity the progress record treats as currently being studied. This establishes "
            "consistency between the records, not that the timetable is academically optimal "
            "or fastest for graduation."
        )
    elif exact_plan_matches:
        conclusion = (
            "Bounded conclusion: the timetable partly aligns with the progress record by exact "
            "code. This evidence cannot label the other codes wrong; an elective or equivalent "
            "identity may not be exposed in this view."
        )
    else:
        conclusion = (
            "Bounded conclusion: no exact-code match is visible between the timetable and the "
            "registered requirement identities. That does not establish that the choices are "
            "wrong because this view does not expose every elective or equivalent relationship."
        )

    lines = [
        conclusion,
        (
            "Exact-code match between the registered timetable and currently registered "
            f"requirement identities: {len(exact_plan_matches)} of {len(registered)} — "
            f"{_shown(exact_plan_matches, language=language)}."
        ),
        (
            "The following remaining-course priority list is not a registration "
            "recommendation; it is a separate prerequisite-chain unlock comparison."
        ),
        (
            "Exact-code overlap with the displayed remaining-course priority list: "
            f"{_shown(exact_priority_overlap, language=language)}."
        ),
    ]
    if not exact_priority_overlap and ranked:
        lines.append(
            "No overlap is expected here: the priority list contains prerequisite-ready "
            "remaining courses and excludes courses the progress record treats as currently "
            "being studied. It therefore does not mean the timetable is wrong."
        )
    if shown_priority_gaps:
        lines.append(
            "Highest displayed priority gaps outside the current timetable: "
            f"{_shown(shown_priority_gaps, language=language)}."
        )
    lines.append(
        "These gaps are ranked only by prerequisite-chain unlock impact, not as registration "
        "recommendations: they do not prove offering, seats, registration permission, or a "
        "better complete timetable."
    )
    return "\n".join(lines)


def render_timetable_build_priority_assessment(
    language: str,
    proposal: Mapping[str, Any],
    progress: Mapping[str, Any],
) -> str:
    """Relate a timetable proposal to priority evidence without upgrading its objective."""

    proposal_codes = _proposal_codes(proposal)
    proposal_set = set(proposal_codes)
    ranked = _priority_codes(progress)
    overlap = [code for code in ranked if code in proposal_set]
    shown_gaps = [code for code in ranked if code not in proposal_set][:5]

    if language == "Arabic":
        lines = [
            (
                "الخلاصة المحدودة للأولوية: فحص الجدولة يثبت مواضع مقررات ضمن القيود "
                "المحددة فقط، ولا يثبت أن محلّل الجدول حسّن موعد التخرج أو رتّب مقرراته "
                "حسب الأولوية. سجل التقدم التالي يستخدم معيارًا منفصلًا هو أثر فتح سلاسل "
                "المتطلبات."
            ),
            ("قائمة الأولوية التالية ليست توصية تسجيل ولا جزءًا من هدف محلّل الجدول."),
            (
                "التداخل الحرفي بين المقررات الموضوعة في أي بديل وقائمة الأولوية المعروضة: "
                f"{_shown(overlap, language=language)}."
            ),
        ]
        if shown_gaps:
            lines.append("والرموز التالية من معيار أولوية مستقل وليست توصية تسجيل.")
            lines.append(
                "أعلى رموز الأولوية المعروضة التي لم تتطابق في هذه المقارنة: "
                f"{_shown(shown_gaps, language=language)}."
            )
        lines.append(
            "لذلك لا توصف البدائل بأنها مُحسّنة للأولوية أو أسرع للتخرج من هذا الدليل؛ "
            "يمكن اعتمادها كخيارات جدولة فقط، وتحتاج مفاضلة أكاديمية منفصلة لإثبات أفضلية "
            "المقررات."
        )
        return "\n".join(lines)

    lines = [
        (
            "Bounded priority conclusion: the timetable check establishes course placement "
            "under the specified constraints only. It does not establish that the solver "
            "optimised graduation timing or course priority. The progress record uses the "
            "separate criterion of prerequisite-chain unlock impact."
        ),
        (
            "The following priority list is not a registration recommendation and was not "
            "the timetable solver's objective."
        ),
        (
            "Exact-code overlap between courses placed in any option and the displayed "
            f"priority list: {_shown(overlap, language=language)}."
        ),
    ]
    if shown_gaps:
        lines.append(
            "The following codes come from the separate priority criterion and are not a "
            "registration recommendation."
        )
        lines.append(
            "Highest displayed priority codes with no match in this comparison: "
            f"{_shown(shown_gaps, language=language)}."
        )
    lines.append(
        "The options therefore must not be described as priority-optimised or faster for "
        "graduation from this evidence; they are timetable options only, and proving a better "
        "course choice requires a separate academic comparison."
    )
    return "\n".join(lines)


def joined_answer_blocks(
    language: str,
    latest: Mapping[str, Mapping[str, Any]],
    *,
    planned_tools: Sequence[str],
    requested_outcomes: Sequence[str],
) -> tuple[tuple[str, str], ...]:
    """Return typed joined blocks licensed by exact V2.1 outcome/tool pairs."""

    tools = set(planned_tools)
    outcomes = set(requested_outcomes)
    blocks: list[tuple[str, str]] = []

    timetable = latest.get("my_timetable")
    progress = latest.get("my_progress")
    if (
        {"current_timetable", "course_priority"} <= outcomes
        and {"my_timetable", "my_progress"} <= tools
        and timetable
        and progress
        and timetable.get("ok")
        and progress.get("ok")
    ):
        heading = "تقييم الاختيارات" if language == "Arabic" else "Course-choice assessment"
        body = render_current_timetable_priority_assessment(language, timetable, progress)
        blocks.append((CURRENT_TIMETABLE_PRIORITY_SCOPE, f"### {heading}\n{body}"))

    proposal = latest.get("build_timetable_proposal")
    if (
        {"timetable_build", "course_priority"} <= outcomes
        and {"build_timetable_proposal", "my_progress"} <= tools
        and proposal
        and progress
        and proposal.get("ok")
        and progress.get("ok")
    ):
        heading = "حدود أولوية المقترح" if language == "Arabic" else "Proposal-priority boundary"
        body = render_timetable_build_priority_assessment(language, proposal, progress)
        blocks.append((TIMETABLE_BUILD_PRIORITY_SCOPE, f"### {heading}\n{body}"))

    return tuple(blocks)


def joined_scope_tools(scope: str) -> tuple[str, ...]:
    """Evidence owners for one synthetic joined validation scope."""

    return JOINED_SCOPE_TOOLS.get(scope, ())


__all__ = [
    "CURRENT_TIMETABLE_PRIORITY_SCOPE",
    "JOINED_SCOPE_TOOLS",
    "TIMETABLE_BUILD_PRIORITY_SCOPE",
    "joined_answer_blocks",
    "joined_scope_tools",
    "localize_timetable_constraint_failure_reason",
    "localize_timetable_day",
    "localize_timetable_unplaced_reason",
    "render_current_timetable_priority_assessment",
    "render_timetable_build_priority_assessment",
]
