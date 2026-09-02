"""Server-owned synthesis for multi-capability Student Advisor V2.1 answers.

Individual capability renderers intentionally describe only their own evidence.
Some questions, however, ask for a relationship between two verified result sets.
Those relationships are computed here rather than left to model prose. Every
statement is a bounded comparison over typed provider-visible fields; it does not
infer eligibility, offering, seats, or an optimal graduation plan.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Any

CURRENT_TIMETABLE_PRIORITY_SCOPE = "current_timetable_priority_assessment"
CURRENT_TIMETABLE_LOAD_POLICY_SCOPE = "current_timetable_load_policy_assessment"
TIMETABLE_BUILD_PRIORITY_SCOPE = "timetable_build_priority_assessment"

JOINED_SCOPE_TOOLS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        CURRENT_TIMETABLE_PRIORITY_SCOPE: ("my_timetable", "my_progress"),
        CURRENT_TIMETABLE_LOAD_POLICY_SCOPE: ("my_timetable", "policy_lookup"),
        TIMETABLE_BUILD_PRIORITY_SCOPE: (
            "build_timetable_proposal",
            "my_progress",
        ),
    }
)

_SEMESTER_RANGE_POLICY_ID = "TU.LOAD.SEMESTER_RANGE"
_EXPECTED_GRADUATE_POLICY_ID = "TU.LOAD.EXPECTED_GRADUATE_REQUEST"
_MAIN_TERMS = frozenset({1, 2})


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
    r"retained baseline has\s+(?P<baseline>\d+)\s+credits?.*?" r"maximum of\s+(?P<maximum>\d+)",
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


def _direct_number(value: Any) -> float | None:
    """Accept an actual finite JSON number, never a number parsed from prose."""

    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _shown_number(value: float) -> str:
    return str(int(value)) if value.is_integer() else f"{value:g}"


def _policy_is_unresolved(policy: Mapping[str, Any]) -> bool:
    return bool(
        policy.get("source_leaves_unresolved")
        or policy.get("source_is_unclear_on")
        or policy.get("open_question")
    )


def _direct_main_term_maximum(policy_result: Mapping[str, Any]) -> float | None:
    """Return one unambiguous structured main-term maximum from direct evidence.

    The policy statement is intentionally not parsed. The comparison is licensed
    only by the typed ``rule.max_value`` on the exact governing policy record.
    """

    direct = _mapping_rows(policy_result.get("direct_policy_evidence"))
    if any(_code(policy.get("policy_id")) == _EXPECTED_GRADUATE_POLICY_ID for policy in direct):
        # That record's 16-hour request ceiling has an explicitly unresolved
        # relationship to the ordinary 19-unit range. Do not choose between them.
        return None

    maxima: set[float] = set()
    for policy in direct:
        if _code(policy.get("policy_id")) != _SEMESTER_RANGE_POLICY_ID:
            continue
        if _policy_is_unresolved(policy):
            return None
        rule = policy.get("rule")
        if not isinstance(rule, Mapping):
            continue
        applies_to = rule.get("applies_to")
        if not isinstance(applies_to, Mapping):
            continue
        if _code(applies_to.get("term_type")) != "MAIN":
            continue
        if _code(applies_to.get("study_system")) != "TWO_SEMESTER":
            continue
        maximum = _direct_number(rule.get("max_value"))
        if maximum is not None and maximum >= 0:
            maxima.add(maximum)
    if len(maxima) != 1:
        return None
    return next(iter(maxima))


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
                "تقييمي: السجل المعروض جدول متوقع للتخطيط وليس تسجيلًا فعليًا؛ "
                "لذلك لا يمكن الحكم منه على المواد التي سجلتها فعليًا."
            )
        if not registered:
            return (
                "تقييمي: لا يحتوي الجدول المعروض على مقررات مسجلة يمكن "
                "مقارنتها بترتيب الأولوية، لذلك لا يمكن وصف اختيارات هذا الفصل بأنها صحيحة "
                "أو غير صحيحة."
            )

        if len(exact_plan_matches) == len(registered):
            conclusion = (
                "تقييمي: تتسق كل رموز مقررات جدولك المسجّل مع هويات متطلبات "
                "يعاملها سجل التقدم على أنها قيد الدراسة حاليًا. هذا يثبت اتساق السجلين، "
                "لكنه لا يثبت أن الجدول هو الاختيار الأكاديمي الأفضل أو الأسرع للتخرج."
            )
        elif exact_plan_matches:
            conclusion = (
                "تقييمي: يتسق الجدول جزئيًا مع سجل التقدم بالمطابقة الحرفية "
                "للرموز. لا تسمح هذه الأدلة وحدها بالحكم بأن الرموز الأخرى خاطئة؛ فقد تكون "
                "لها هوية اختيارية أو بديلة لا تظهر في هذا العرض."
            )
        else:
            conclusion = (
                "تقييمي: لا يظهر تطابق حرفي بين رموز الجدول وهويات المتطلبات "
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
            "My assessment: the displayed record is an expected planning timetable, "
            "not actual registration, so it cannot assess which courses you actually registered."
        )
    if not registered:
        return (
            "My assessment: the displayed timetable contains no registered "
            "course rows to compare with the priority ranking, so it cannot label this term's "
            "choices right or wrong."
        )

    if len(exact_plan_matches) == len(registered):
        conclusion = (
            "My assessment: every registered timetable code aligns with a requirement "
            "identity the progress record treats as currently being studied. This establishes "
            "consistency between the records, not that the timetable is academically optimal "
            "or fastest for graduation."
        )
    elif exact_plan_matches:
        conclusion = (
            "My assessment: the timetable partly aligns with the progress record by exact "
            "code. This evidence cannot label the other codes wrong; an elective or equivalent "
            "identity may not be exposed in this view."
        )
    else:
        conclusion = (
            "My assessment: no exact-code match is visible between the timetable and the "
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


def render_current_timetable_load_policy_assessment(
    language: str,
    timetable: Mapping[str, Any],
    policy_result: Mapping[str, Any],
) -> str | None:
    """Compare a registered main-term load with one direct structured maximum.

    Returning ``None`` is deliberate fail-closed behaviour. Expected planning
    timetables, summer or unknown terms, prose-only limits, and unresolved
    expected-graduate evidence do not license this comparison.
    """

    if bool(timetable.get("is_expected_plan")):
        return None
    if _code(timetable.get("schedule_kind")) != "REGISTERED":
        return None
    term = timetable.get("term")
    if isinstance(term, bool) or term not in _MAIN_TERMS:
        return None
    registered = _direct_number(timetable.get("registered_credit_hours"))
    maximum = _direct_main_term_maximum(policy_result)
    if registered is None or registered < 0 or maximum is None:
        return None

    registered_text = _shown_number(registered)
    maximum_text = _shown_number(maximum)
    difference_text = _shown_number(abs(maximum - registered))

    if language == "Arabic":
        if registered < maximum:
            relation = f"لذلك يقل عبؤك عن الرقم التنظيمي بـ {difference_text} ساعة معتمدة."
        elif registered == maximum:
            relation = "لذلك يساوي عبؤك ذلك الحد الأعلى تمامًا."
        else:
            relation = (
                f"لذلك يزيد العبء المسجّل على ذلك الحد الأعلى بـ {difference_text} "
                "ساعة معتمدة؛ وهذا وصف للفارق في السجلين وليس حكمًا على السماح بالتسجيل."
            )
        return "\n".join(
            (
                f"عبء جدولك المسجّل هو {registered_text} ساعة معتمدة.",
                (
                    "يضع السجل التنظيمي المباشر للفصل الرئيس حدًا أعلى قدره "
                    f"{maximum_text} ساعة معتمدة [{_SEMESTER_RANGE_POLICY_ID}]."
                ),
                relation,
                (
                    "العبء الدراسي وحده لا يثبت جودة اختيار مقررات الجدول أكاديميًا، "
                    "ولا يثبت أن الجدول يسرّع التخرج."
                ),
            )
        )

    if registered < maximum:
        relation = f"Your load is therefore {difference_text} credit hours below that maximum."
    elif registered == maximum:
        relation = "Your load is therefore exactly at that maximum."
    else:
        relation = (
            f"The recorded load is therefore {difference_text} credit hours above that maximum; "
            "this describes the difference between the records, not registration permission."
        )
    return "\n".join(
        (
            f"Your registered timetable load is {registered_text} credit hours.",
            (
                "The directly governing main-term record sets a maximum of "
                f"{maximum_text} credit hours [{_SEMESTER_RANGE_POLICY_ID}]."
            ),
            relation,
            (
                "Credit load alone does not establish that the timetable has academically "
                "well-chosen courses or that it is faster for graduation."
            ),
        )
    )


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
                "تقييمي: خيارات الجدول تحقق مواضع مقررات ضمن القيود "
                "المحددة فقط، ولا يثبت أن إنشاء هذه الخيارات حسّن موعد التخرج أو رتّب المقررات "
                "حسب الأولوية. سجل التقدم التالي يستخدم معيارًا منفصلًا هو أثر فتح سلاسل "
                "المتطلبات."
            ),
            ("قائمة الأولوية التالية ليست توصية تسجيل ولم تكن ضمن معيار إنشاء الجدول."),
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
            "My assessment: the timetable options establish course placement under the "
            "specified constraints only. They do not establish that the timetable search "
            "optimised graduation timing or course priority. The progress record uses the "
            "separate criterion of prerequisite-chain unlock impact."
        ),
        (
            "The following priority list is not a registration recommendation and was not "
            "part of the timetable search criterion."
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
        heading = "تقييمي لاختياراتك" if language == "Arabic" else "My assessment"
        body = render_current_timetable_priority_assessment(language, timetable, progress)
        blocks.append((CURRENT_TIMETABLE_PRIORITY_SCOPE, f"### {heading}\n{body}"))

    policy_result = latest.get("policy_lookup")
    if (
        outcomes == {"current_timetable", "policy_rule"}
        and tools == {"my_timetable", "policy_lookup"}
        and timetable
        and policy_result
        and timetable.get("ok")
        and policy_result.get("ok")
    ):
        body = render_current_timetable_load_policy_assessment(
            language,
            timetable,
            policy_result,
        )
        if body:
            heading = (
                "مقارنة عبئك الدراسي بالحد الأعلى"
                if language == "Arabic"
                else "How your credit load compares"
            )
            blocks.append((CURRENT_TIMETABLE_LOAD_POLICY_SCOPE, f"### {heading}\n{body}"))

    proposal = latest.get("build_timetable_proposal")
    if (
        {"timetable_build", "course_priority"} <= outcomes
        and {"build_timetable_proposal", "my_progress"} <= tools
        and proposal
        and progress
        and proposal.get("ok")
        and progress.get("ok")
    ):
        heading = "مفاضلة أولوية المقترح" if language == "Arabic" else "Priority trade-off"
        body = render_timetable_build_priority_assessment(language, proposal, progress)
        blocks.append((TIMETABLE_BUILD_PRIORITY_SCOPE, f"### {heading}\n{body}"))

    return tuple(blocks)


def joined_scope_tools(scope: str) -> tuple[str, ...]:
    """Evidence owners for one synthetic joined validation scope."""

    return JOINED_SCOPE_TOOLS.get(scope, ())


__all__ = [
    "CURRENT_TIMETABLE_LOAD_POLICY_SCOPE",
    "CURRENT_TIMETABLE_PRIORITY_SCOPE",
    "JOINED_SCOPE_TOOLS",
    "TIMETABLE_BUILD_PRIORITY_SCOPE",
    "joined_answer_blocks",
    "joined_scope_tools",
    "localize_timetable_constraint_failure_reason",
    "localize_timetable_day",
    "localize_timetable_unplaced_reason",
    "render_current_timetable_load_policy_assessment",
    "render_current_timetable_priority_assessment",
    "render_timetable_build_priority_assessment",
]
