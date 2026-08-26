"""Deterministic renderers for V2.1 evidence without safe V2 presentations.

These functions do not interpret a student's request and do not make academic
decisions.  They only turn already-authorized, channel-projected capability rows
into student-facing text.  Keeping the mapping field-by-field prevents an LLM
from changing a verified course name, prerequisite relation, or plan status.
"""

from __future__ import annotations

from typing import Any


def _text(value: Any) -> str:
    return str(value or "").strip()


def _number(value: Any) -> str:
    if isinstance(value, bool) or value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _course_label(row: dict[str, Any]) -> str:
    code = _text(row.get("course_code") or row.get("candidate_code")).upper()
    name = _text(row.get("course_name") or row.get("candidate_name"))
    return code + (f" — {name}" if name else "")


def render_lookup_course(language: str, row: dict[str, Any]) -> str:
    """Render only catalogue fields returned by ``lookup_course``."""

    if not row.get("ok"):
        return (
            "تعذّر التحقق من سجل المقرر المطلوب."
            if language == "Arabic"
            else "I could not verify the requested course record."
        )

    courses = [item for item in row.get("courses") or [] if isinstance(item, dict)]
    if courses:
        separator = "؛ " if language == "Arabic" else "; "
        lines = [
            ("المقررات المطابقة في السجل:" if language == "Arabic" else "Matching course records:")
        ]
        for course in courses:
            label = _course_label(course)
            if not label:
                continue
            details: list[str] = []
            credits = _number(course.get("credit_hours"))
            if credits:
                details.append(
                    f"{credits} ساعات معتمدة" if language == "Arabic" else f"{credits} credits"
                )
            programs = [_text(value).upper() for value in course.get("programs") or []]
            programs = [value for value in programs if value]
            if programs:
                details.append(
                    "البرامج: " + "، ".join(programs)
                    if language == "Arabic"
                    else "programs: " + ", ".join(programs)
                )
            slots = [_text(value).upper() for value in course.get("fulfills_elective_slots") or []]
            slots = [value for value in slots if value]
            if slots:
                details.append(
                    "يحقق خانات الاختيار: " + "، ".join(slots)
                    if language == "Arabic"
                    else "fulfills elective slots: " + ", ".join(slots)
                )
            lines.append(f"- {label}" + (f" — {separator.join(details)}" if details else ""))
        if len(lines) > 1:
            return "\n".join(lines)

    unknown = _text(row.get("unknown_query")).upper()
    suggestions = [item for item in row.get("did_you_mean") or [] if isinstance(item, dict)]
    if unknown:
        lines = [
            (
                f"لم أجد سجلًا معتمدًا لرمز المقرر {unknown}."
                if language == "Arabic"
                else f"I found no approved catalogue record for course code {unknown}."
            )
        ]
        labels = [_course_label(item) for item in suggestions]
        labels = [label for label in labels if label]
        if labels:
            lines.append(
                "رموز قريبة موجودة في السجل: " + "، ".join(labels) + "."
                if language == "Arabic"
                else "Nearby codes that do exist in the catalogue: " + ", ".join(labels) + "."
            )
        return "\n".join(lines)

    query = _text(row.get("query"))
    return (
        f"لم يظهر تطابق موثق لعبارة البحث «{query}»."
        if language == "Arabic" and query
        else (
            "لم يظهر تطابق موثق لعبارة البحث."
            if language == "Arabic"
            else (
                f"No verified catalogue match was found for “{query}”."
                if query
                else "No verified catalogue match was found."
            )
        )
    )


def _prerequisite_values(value: Any) -> list[str]:
    if not isinstance(value, list | tuple):
        return []
    out: list[str] = []
    for item in value:
        if isinstance(item, dict):
            candidate = _text(
                item.get("course_code") or item.get("code") or item.get("requirement")
            )
        else:
            candidate = _text(item)
        if candidate and candidate not in out:
            out.append(candidate)
    return out


def _prerequisite_line(language: str, prerequisites: list[str]) -> str:
    if prerequisites:
        return (
            "المتطلبات السابقة المسجلة: " + "، ".join(prerequisites)
            if language == "Arabic"
            else "Recorded prerequisites: " + ", ".join(prerequisites)
        )
    return (
        "لا تظهر متطلبات سابقة مسجلة لهذا الخيار."
        if language == "Arabic"
        else "No prerequisite is recorded for this option."
    )


def render_course_prerequisites(language: str, row: dict[str, Any]) -> str:
    """Render the tagged prerequisite result without inferring eligibility."""

    if not row.get("ok"):
        return (
            "تعذّر التحقق من المتطلبات السابقة للمقرر المطلوب."
            if language == "Arabic"
            else "I could not verify prerequisites for the requested course."
        )

    code = _text(row.get("course_code")).upper()
    options = [item for item in row.get("options") or [] if isinstance(item, dict)]
    if row.get("is_elective_placeholder") and options:
        lines = [
            (
                f"{code} خانة اختيار في الخطة، وهذه خياراتها المسجلة:"
                if language == "Arabic"
                else f"{code} is an elective plan slot. Its recorded options are:"
            )
        ]
        for option in options:
            label = _course_label(option)
            credits = _number(option.get("credit_hours"))
            if credits:
                label += (
                    f" — {credits} ساعات معتمدة"
                    if language == "Arabic"
                    else f" — {credits} credits"
                )
            prereqs = _prerequisite_values(option.get("prerequisites"))
            lines.append(f"- {label} — {_prerequisite_line(language, prereqs)}")
        lines.append(
            "لكل مقرر فعلي متطلباته؛ خانة الاختيار نفسها ليست مقررًا."
            if language == "Arabic"
            else "Each concrete course has its own prerequisites; the slot itself is not a course."
        )
        return "\n".join(lines)

    programs = [item for item in row.get("per_program") or [] if isinstance(item, dict)]
    if not programs:
        return (
            f"لا يظهر للمقرر {code} سجل متطلبات ضمن خطة برنامجك."
            if language == "Arabic" and code
            else (
                "لا يظهر سجل متطلبات ضمن خطة برنامجك."
                if language == "Arabic"
                else (
                    f"No prerequisite record for {code} appears in your programme plan."
                    if code
                    else "No prerequisite record appears in your programme plan."
                )
            )
        )

    lines = [
        (
            f"المتطلبات المسجلة للمقرر {code}."
            if language == "Arabic"
            else f"Recorded prerequisites for {code}."
        )
    ]
    separator = "؛ " if language == "Arabic" else "; "
    for program in programs:
        program_code = _text(program.get("program")).upper()
        name = _text(program.get("course_name"))
        heading = program_code or (
            "البرنامج المسجل" if language == "Arabic" else "recorded programme"
        )
        if name:
            heading += f" — {name}"
        details = [_prerequisite_line(language, _prerequisite_values(program.get("prerequisites")))]
        credits = _number(program.get("credit_hours"))
        if credits:
            details.append(
                f"الساعات المعتمدة: {credits}" if language == "Arabic" else f"credits: {credits}"
            )
        level = _number(program.get("programme_term"))
        if level:
            details.append(
                f"مستوى الخطة: {level}" if language == "Arabic" else f"plan level: {level}"
            )
        slots = [_text(value).upper() for value in program.get("fulfills_elective_slots") or []]
        slots = [value for value in slots if value]
        if slots:
            details.append(
                "يحقق خانات الاختيار: " + "، ".join(slots)
                if language == "Arabic"
                else "fulfills elective slots: " + ", ".join(slots)
            )
        lines.append(f"- {heading}: " + separator.join(details))
    lines.append(
        "هذه بيانات المتطلبات فقط؛ ولا تثبت طرح شعبة أو وجود مقعد أو السماح بالتسجيل."
        if language == "Arabic"
        else "Prerequisite data alone does not prove offering, seats, or registration permission."
    )
    return "\n".join(lines)


_STATUS_EN = {
    "passed": "passed",
    "studying": "studying in the recorded academic state",
    "failed": "failed in the recorded academic state",
    "not_taken": "not taken",
}
_STATUS_AR = {
    "passed": "مجتاز",
    "studying": "قيد الدراسة في السجل الأكاديمي",
    "failed": "راسب في السجل الأكاديمي",
    "not_taken": "لم يُدرس بعد",
}


def render_plan_by_term(language: str, row: dict[str, Any]) -> str:
    """Render degree-plan levels and exact statuses from ``my_plan_by_term``."""

    if not row.get("ok"):
        return (
            "تعذّر التحقق من خطة المقررات الخاصة بك."
            if language == "Arabic"
            else "I could not verify your degree plan."
        )

    terms = [item for item in row.get("terms") or [] if isinstance(item, dict)]
    requested_level = _number(row.get("plan_level"))
    if not terms:
        return (
            f"لا تتضمن الخطة المسجلة مستوى رقم {requested_level}."
            if language == "Arabic" and requested_level
            else (
                "لا تظهر مستويات مقررات في الخطة المسجلة."
                if language == "Arabic"
                else (
                    f"The recorded plan has no level {requested_level}."
                    if requested_level
                    else "No course levels appear in the recorded degree plan."
                )
            )
        )

    raw_summary = row.get("summary")
    summary: dict[str, Any] = raw_summary if isinstance(raw_summary, dict) else {}
    lines: list[str] = []
    summary_parts: list[str] = []
    for key, ar_label, en_label in (
        ("passed", "مجتاز", "passed"),
        ("studying", "قيد الدراسة", "studying"),
        ("failed", "راسب", "failed"),
        (
            "not_taken_can_register",
            "غير مدروس ومتطلباته مستوفاة",
            "not taken, prerequisites satisfied",
        ),
        (
            "not_taken_locked",
            "غير مدروس ومحجوب بمتطلبات",
            "not taken, prerequisite-blocked",
        ),
    ):
        value = _number(summary.get(key))
        if value:
            summary_parts.append(
                f"{ar_label}: {value}" if language == "Arabic" else f"{en_label}: {value}"
            )
    if summary_parts:
        lines.append(
            "ملخص الخطة — " + "؛ ".join(summary_parts) + "."
            if language == "Arabic"
            else "Plan summary — " + "; ".join(summary_parts) + "."
        )

    rendered_courses = 0
    max_courses = 120
    truncated = False
    separator = "؛ " if language == "Arabic" else "; "
    for term in terms[:10]:
        level = _number(term.get("term")) or "?"
        lines.append(
            f"**المستوى {level}:**" if language == "Arabic" else f"**Plan level {level}:**"
        )
        courses = [item for item in term.get("courses") or [] if isinstance(item, dict)]
        if not courses:
            lines.append(
                "- لا توجد مقررات مسجلة." if language == "Arabic" else "- No recorded courses."
            )
            continue
        for course in courses:
            if rendered_courses >= max_courses:
                truncated = True
                break
            rendered_courses += 1
            code = _text(course.get("course_code")).upper()
            if not code:
                continue
            status_key = _text(course.get("status")).lower()
            status = (
                _STATUS_AR.get(status_key, "الحالة غير محددة في السجل")
                if language == "Arabic"
                else _STATUS_EN.get(status_key, "status not specified in the record")
            )
            credits = _number(course.get("credit_hours"))
            parts = [status]
            if credits:
                parts.append(
                    f"{credits} ساعات معتمدة" if language == "Arabic" else f"{credits} credits"
                )
            readiness = course.get("prerequisites_satisfied")
            if isinstance(readiness, bool):
                parts.append(
                    (
                        "المتطلبات السابقة المسجلة مستوفاة"
                        if readiness
                        else "بعض المتطلبات السابقة المسجلة غير مستوفاة"
                    )
                    if language == "Arabic"
                    else (
                        "recorded prerequisites satisfied"
                        if readiness
                        else "recorded prerequisites not satisfied"
                    )
                )
            missing = _prerequisite_values(course.get("missing_prereqs"))
            if missing:
                parts.append(
                    "المتطلبات غير المستوفاة: " + "، ".join(missing)
                    if language == "Arabic"
                    else "missing prerequisites: " + ", ".join(missing)
                )
            lines.append(f"- {code} — " + separator.join(parts))
        if truncated:
            break
    if truncated:
        lines.append(
            "عُرض أول 120 مقررًا فقط لتجنب رسالة طويلة جدًا."
            if language == "Arabic"
            else "Only the first 120 courses are shown to keep the response bounded."
        )
    lines.append(
        "استيفاء المتطلبات السابقة لا يعني طرح شعبة أو وجود مقعد أو السماح بالتسجيل."
        if language == "Arabic"
        else "Prerequisite readiness does not prove offering, seats, or registration permission."
    )
    return "\n".join(lines)


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _mapping_rows(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list | tuple):
        return []
    return [item for item in value if isinstance(item, dict)]


def _string_values(value: Any, *, upper: bool = False) -> list[str]:
    if not isinstance(value, list | tuple):
        return []
    values: list[str] = []
    for item in value:
        candidate = _text(item)
        if upper:
            candidate = candidate.upper()
        if candidate and candidate not in values:
            values.append(candidate)
    return values


_BASELINE_AR = {
    "REGISTERED": "الجدول المسجّل فعليًا",
    "EXPECTED_PLAN": "الجدول المتوقع، وليس تسجيلًا فعليًا",
    "MIXED_REVIEW_REQUIRED": "مصادر مختلطة تحتاج إلى مراجعة",
    "NOT_EVALUATED": "لم يُقيّم خط الأساس",
    "NOT_DETERMINABLE": "تعذّر تحديد خط الأساس",
}
_BASELINE_EN = {
    "REGISTERED": "the actually registered timetable",
    "EXPECTED_PLAN": "the expected plan, not actual registration",
    "MIXED_REVIEW_REQUIRED": "mixed sources requiring review",
    "NOT_EVALUATED": "baseline not evaluated",
    "NOT_DETERMINABLE": "baseline not determinable",
}


def _compound_context_lines(language: str, row: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    planning_term = _text(row.get("planning_term"))
    if planning_term:
        lines.append(
            f"الفصل الذي جرى تحليله: {planning_term}."
            if language == "Arabic"
            else f"Planning term analysed: {planning_term}."
        )
    baseline = _text(row.get("baseline_kind")).upper()
    if baseline:
        label = (
            _BASELINE_AR.get(baseline, "حالة خط الأساس غير محددة")
            if language == "Arabic"
            else _BASELINE_EN.get(baseline, "baseline status not specified")
        )
        lines.append(f"خط الأساس: {label}." if language == "Arabic" else f"Baseline: {label}.")
    pins = _mapping_rows(_mapping(row.get("constraints")).get("pinned_sections"))
    pin_labels = [
        f"{_text(pin.get('course_code')).upper()}-{_text(pin.get('section_label')).upper()}"
        for pin in pins
        if _text(pin.get("course_code")) and _text(pin.get("section_label"))
    ]
    if pin_labels:
        lines.append(
            "الشُعب المثبتة والمتحقق من بقائها في خط الأساس: " + "، ".join(pin_labels) + "."
            if language == "Arabic"
            else "Verified retained section pins: " + ", ".join(pin_labels) + "."
        )
    return lines


def _compound_read_only_footer(language: str) -> list[str]:
    if language == "Arabic":
        return [
            (
                "هذه نتيجة بحث محدود وللقراءة فقط؛ لم يُضف أو يُحذف أو يُحفظ "
                "أو يُطبّق أو يُسجّل أي مقرر أو شعبة."
            ),
            (
                "الملاءمة في اللقطة المسجلة لا تثبت طرحًا حيًا أو مقعدًا أو إذن "
                "تسجيل؛ وأي تغيير ينفذه الطالب يدويًا في بوابة الجامعة بعد التحقق."
            ),
        ]
    return [
        (
            "This is a bounded, read-only result; no course or section was added, "
            "dropped, saved, applied, or registered."
        ),
        (
            "Fit in the recorded snapshot does not prove a live offering, seat, or "
            "registration permission; the student must make any change manually in "
            "the university portal after verification."
        ),
    ]


def _bounded_negative_line(language: str) -> str:
    return (
        "لم ينتج الفحص المحدود نتيجة إيجابية موثقة."
        if language == "Arabic"
        else "The bounded check did not produce a verified positive result."
    )


_TIMING_AR = {
    "EARLIER": "أبكر",
    "FORECAST_COMPLETED": "تحول التوقع إلى مكتمل",
    "SAME": "لم يتغير عدد الفصول المتوقع",
    "UNRESOLVED_IMPROVEMENT": "تحسنت العوائق لكن بقي التوقيت غير محسوم",
    "LATER": "أبعد",
    "FORECAST_BECAME_UNRESOLVED": "أصبح التوقع غير محسوم",
    "UNRESOLVED_WORSE": "ازدادت العوائق مع بقاء التوقيت غير محسوم",
    "NOT_DETERMINABLE": "غير قابل للتحديد",
}
_TIMING_EN = {
    "EARLIER": "earlier",
    "FORECAST_COMPLETED": "forecast became complete",
    "SAME": "no change in forecast term count",
    "UNRESOLVED_IMPROVEMENT": "blockers improved but timing remains unresolved",
    "LATER": "later",
    "FORECAST_BECAME_UNRESOLVED": "forecast became unresolved",
    "UNRESOLVED_WORSE": "blockers worsened while timing remains unresolved",
    "NOT_DETERMINABLE": "not determinable",
}


def _graduation_parts(language: str, value: Any) -> list[str]:
    graduation = _mapping(value)
    parts: list[str] = []
    timing = _text(graduation.get("timing_effect")).upper()
    if timing:
        label = (
            _TIMING_AR.get(timing, "أثر غير محدد")
            if language == "Arabic"
            else _TIMING_EN.get(timing, "effect not specified")
        )
        parts.append(
            f"أثر التخرج: {label}" if language == "Arabic" else f"graduation effect: {label}"
        )
    for key, ar_label, en_label in (
        ("term_difference", "فرق الفصول المسجل", "recorded term difference"),
        ("terms_saved", "الفصول التي وفرها السيناريو", "scenario terms saved"),
        (
            "estimated_additional_terms",
            "الفصول الإضافية المقدرة",
            "estimated additional terms",
        ),
        (
            "lower_bound_additional_terms",
            "الحد الأدنى للفصول الإضافية",
            "lower-bound additional terms",
        ),
    ):
        number = _number(graduation.get(key))
        if number:
            parts.append(
                f"{ar_label}: {number}" if language == "Arabic" else f"{en_label}: {number}"
            )
    for key, ar_label, en_label in (
        ("blockers_resolved", "عوائق حُلّت", "blockers resolved"),
        ("blockers_improved", "عوائق تحسنت", "blockers improved"),
        ("blockers_introduced", "عوائق أضيفت", "blockers introduced"),
        (
            "affected_future_course_codes",
            "مقررات مستقبلية متأثرة",
            "affected future courses",
        ),
    ):
        codes = _string_values(graduation.get(key), upper=True)
        if codes:
            parts.append(
                f"{ar_label}: " + "، ".join(codes)
                if language == "Arabic"
                else f"{en_label}: " + ", ".join(codes)
            )
    return parts


def _addition_search_line(language: str, row: dict[str, Any]) -> str:
    search = _mapping(row.get("search"))
    evaluated = _number(search.get("candidates_evaluated")) or "0"
    feasible = _number(search.get("feasible_candidates_found")) or "0"
    limit = _number(search.get("candidate_limit"))
    truncated = search.get("search_truncated") is True
    if language == "Arabic":
        text = f"البحث المحدود قيّم {evaluated} مرشحًا ووجد {feasible} مرشحًا ملائمًا"
        if limit:
            text += f" ضمن حد أقصى قدره {limit}"
        text += "."
        if truncated:
            text += " بلغ البحث حد المرشحين، لذلك لم يشمل كل المرشحين الممكنين."
        text += " بحث بدائل الجدول غير شامل لكل التركيبات الممكنة."
        return text
    text = f"The bounded search evaluated {evaluated} candidate(s) and found {feasible} feasible"
    if limit:
        text += f", within a limit of {limit}"
    text += "."
    if truncated:
        text += " The candidate limit was reached, so not every possible candidate was searched."
    text += " The timetable-alternative search is not exhaustive."
    return text


def _render_addition_candidate(language: str, candidate: dict[str, Any]) -> str:
    label = _course_label(candidate) or (
        "مقرر بلا رمز مسجل" if language == "Arabic" else "course without a recorded code"
    )
    details: list[str] = []
    rank = _number(candidate.get("rank"))
    if rank:
        details.append(f"الترتيب: {rank}" if language == "Arabic" else f"rank: {rank}")
    credits = _number(candidate.get("credit_hours"))
    if credits:
        details.append(f"{credits} ساعات معتمدة" if language == "Arabic" else f"{credits} credits")
    eligibility = _text(_mapping(candidate.get("eligibility")).get("status")).upper()
    if eligibility == "PREREQUISITES_SATISFIED":
        details.append(
            "المتطلبات السابقة المسجلة مستوفاة"
            if language == "Arabic"
            else "recorded prerequisites satisfied"
        )
    official = _mapping(candidate.get("official_recommendation"))
    if official.get("included") is True:
        official_rank = _number(official.get("rank"))
        details.append(
            "ضمن التوصية الرسمية" + (f" بالترتيب {official_rank}" if official_rank else "")
            if language == "Arabic"
            else "included in the official recommendation"
            + (f" at rank {official_rank}" if official_rank else "")
        )
    impact = _mapping(candidate.get("unlock_impact"))
    direct = _number(impact.get("sole_remaining_prerequisite_count"))
    chain = _number(impact.get("on_prerequisite_chain_of_count"))
    if direct:
        details.append(
            f"مقررات تنتظره وحده: {direct}"
            if language == "Arabic"
            else f"courses waiting on it alone: {direct}"
        )
    if chain:
        details.append(
            f"مقررات يقع في سلسلة متطلباتها: {chain}"
            if language == "Arabic"
            else f"courses with it on their prerequisite chain: {chain}"
        )
    timetable = _mapping(candidate.get("timetable"))
    section_rows = _mapping_rows(timetable.get("clash_free_sections"))
    section_labels = [
        _text(section.get("section")).upper()
        for section in section_rows
        if _text(section.get("section"))
    ]
    section_count = _number(timetable.get("clash_free_section_count"))
    if section_count:
        detail = (
            f"شُعب ملائمة في اللقطة المسجلة: {section_count}"
            if language == "Arabic"
            else f"recorded-snapshot fitting sections: {section_count}"
        )
        if section_labels:
            detail += " (" + ", ".join(section_labels) + ")"
        details.append(detail)
    details.extend(_graduation_parts(language, candidate.get("graduation")))
    separator = "؛ " if language == "Arabic" else "; "
    return f"- {label}" + (f" — {separator.join(details)}" if details else "")


def render_recommend_feasible_course_addition(language: str, row: dict[str, Any]) -> str:
    """Render one typed, bounded course-addition outcome without model prose."""

    lines: list[str] = []
    if not row.get("ok"):
        lines.extend(
            [
                _bounded_negative_line(language),
                (
                    "تعذّر تحديد إضافة مقرر من الأدلة الموثقة المتاحة."
                    if language == "Arabic"
                    else "A course addition could not be determined from the available verified evidence."
                ),
            ]
        )
        return "\n".join([*lines, *_compound_read_only_footer(language)])

    status = _text(row.get("status")).upper()
    if status == "RECOMMENDATION_FOUND":
        lines.append(
            "وُجدت إضافة مقرر ملائمة ضمن البحث المحدود واللقطة المسجلة:"
            if language == "Arabic"
            else "A feasible course addition was found within the bounded recorded-snapshot search:"
        )
        candidates = _mapping_rows(row.get("ranked_feasible_additions"))
        recommended = _mapping(row.get("recommended_addition"))
        recommended_code = _text(recommended.get("course_code")).upper()
        if recommended and not any(
            _text(candidate.get("course_code")).upper() == recommended_code
            for candidate in candidates
        ):
            candidates.insert(0, recommended)
        lines.extend(
            _render_addition_candidate(language, candidate) for candidate in candidates[:10]
        )
    elif status == "NO_ELIGIBLE_CANDIDATES":
        lines.append(
            "لم يعثر نطاق المرشحين الذي جرى فحصه على مقرر مستوفٍ للمتطلبات السابقة."
            if language == "Arabic"
            else "The evaluated candidate set contained no course with verified prerequisite readiness."
        )
    elif status == "NO_FEASIBLE_ADDITION_IN_RECORDED_SNAPSHOT":
        lines.append(
            (
                "لم يثبت البحث المحدود وجود إضافة مقرر واحدة ملائمة في اللقطة المسجلة؛ "
                "وهذا لا يثبت استحالة كل الإضافات خارج نطاق البحث."
            )
            if language == "Arabic"
            else (
                "The bounded search verified no feasible single-course addition in the recorded "
                "snapshot; this does not prove that every addition outside the search is impossible."
            )
        )
    elif status == "NO_VERIFIED_FASTER_GRADUATION_IN_BOUNDED_SEARCH":
        lines.append(
            (
                "وُجدت إضافات ملائمة في اللقطة المسجلة، لكن لم يثبت البحث المحدود أن "
                "أيًا منها يقدّم توقّع التخرج إلى وقت أبكر."
            )
            if language == "Arabic"
            else (
                "Feasible additions existed in the recorded snapshot, but the bounded search "
                "did not verify an earlier graduation forecast for any of them."
            )
        )
    elif status == "CONSTRAINTS_UNSATISFIED":
        baseline_credits = _number(row.get("baseline_credit_hours"))
        effective_cap = _number(_mapping(row.get("constraints")).get("effective_max_credits"))
        lines.append(
            (
                "ساعات خط الأساس المحتفظ به تتجاوز الحد الفعلي، لذلك لا يمكن لأي إضافة "
                f"أن تحقق هذا القيد (الخط الأساس: {baseline_credits or '?'}؛ الحد: "
                f"{effective_cap or '?'})."
            )
            if language == "Arabic"
            else (
                "The retained baseline credit load exceeds the effective limit, so no addition "
                f"can satisfy this constraint (baseline: {baseline_credits or '?'}; limit: "
                f"{effective_cap or '?'})."
            )
        )
    else:
        lines.append(
            "لم تكن نتيجة إضافة المقرر قابلة للتحديد من الأدلة المسجلة."
            if language == "Arabic"
            else "The course-addition outcome was not determinable from the recorded evidence."
        )
    if status != "RECOMMENDATION_FOUND":
        lines.insert(0, _bounded_negative_line(language))
    lines.extend(_compound_context_lines(language, row))
    lines.append(_addition_search_line(language, row))
    lines.extend(_compound_read_only_footer(language))
    return "\n".join(lines)


_DROP_IMPACT_AR = {
    "NO_DETECTED_DELAY": "لم يرصد السيناريو تأخيرًا",
    "NO_DETECTED_TERM_DELAY": "لم يرصد السيناريو تأخيرًا بعدد الفصول، مع احتمال آثار أكاديمية أخرى",
    "DELAYED": "أظهر السيناريو تأخيرًا",
    "FORECAST_WORSE": "أصبح توقع التخرج أسوأ أو غير محسوم",
    "NOT_DETERMINABLE": "تعذّر تحديد الأثر",
}
_DROP_IMPACT_EN = {
    "NO_DETECTED_DELAY": "the scenario detected no delay",
    "NO_DETECTED_TERM_DELAY": "the scenario detected no term-count delay, but other academic effects may remain",
    "DELAYED": "the scenario indicates a delay",
    "FORECAST_WORSE": "the graduation forecast became worse or unresolved",
    "NOT_DETERMINABLE": "impact not determinable",
}


def _render_drop_row(language: str, drop: dict[str, Any]) -> str:
    label = _course_label(drop) or (
        "مقرر بلا رمز مسجل" if language == "Arabic" else "course without a recorded code"
    )
    details: list[str] = []
    rank = _number(drop.get("rank"))
    if rank:
        details.append(f"الترتيب: {rank}" if language == "Arabic" else f"rank: {rank}")
    credits = _number(drop.get("credit_hours"))
    if credits:
        details.append(f"{credits} ساعات معتمدة" if language == "Arabic" else f"{credits} credits")
    sections = _string_values(drop.get("sections"), upper=True)
    if sections:
        details.append(
            "الشُعب المسجلة: " + "، ".join(sections)
            if language == "Arabic"
            else "registered sections: " + ", ".join(sections)
        )
    impact = _text(drop.get("impact_status")).upper()
    if impact:
        details.append(
            _DROP_IMPACT_AR.get(impact, "حالة الأثر غير محددة")
            if language == "Arabic"
            else _DROP_IMPACT_EN.get(impact, "impact status not specified")
        )
    details.extend(_graduation_parts(language, drop.get("graduation")))
    separator = "؛ " if language == "Arabic" else "; "
    return f"- {label}" + (f" — {separator.join(details)}" if details else "")


def render_rank_current_course_drop_impact(language: str, row: dict[str, Any]) -> str:
    """Render a typed pure-drop ranking without claiming a drop is harmless."""

    lines: list[str] = []
    if not row.get("ok"):
        lines.extend(
            [
                _bounded_negative_line(language),
                (
                    "تعذّر تقييم أثر حذف مقرر من الأدلة الموثقة المتاحة."
                    if language == "Arabic"
                    else "Course-drop impact could not be evaluated from the available verified evidence."
                ),
            ]
        )
        return "\n".join([*lines, *_compound_read_only_footer(language)])

    status = _text(row.get("status")).upper()
    if status == "RANKING_AVAILABLE":
        objective = _text(row.get("objective")).lower()
        objective_labels_ar = {
            "least_graduation_delay": "أقل تأخير مرصود في التخرج",
            "lowest_academic_priority": "أدنى أولوية أكاديمية موثقة",
            "prerequisite_continuity": "أفضل محافظة على استمرارية المتطلبات",
            "balanced": "الترتيب المتوازن الأقل ضررًا",
        }
        objective_labels_en = {
            "least_graduation_delay": "least detected graduation delay",
            "lowest_academic_priority": "lowest verified academic priority",
            "prerequisite_continuity": "best preservation of prerequisite continuity",
            "balanced": "balanced least-harmful ranking",
        }
        lines.append(
            "ترتيب آثار حذف مقرر واحد من الجدول المسجّل فعليًا:"
            if language == "Arabic"
            else "Ranked impact of dropping one course from the actually registered timetable:"
        )
        drops = _mapping_rows(row.get("ranked_drop_impacts"))
        top_ranked = _mapping(row.get("top_ranked_drop_candidate"))
        top_code = _text(top_ranked.get("course_code")).upper()
        if top_ranked and not any(
            _text(drop.get("course_code")).upper() == top_code for drop in drops
        ):
            drops.insert(0, top_ranked)
        if top_code:
            lines.append(
                f"المرشح الأعلى وفق هدف «{objective_labels_ar.get(objective, 'الهدف المحدد')}»: {top_code}."
                if language == "Arabic"
                else (
                    "Top-ranked candidate for "
                    f"{objective_labels_en.get(objective, 'the selected objective')}: "
                    f"{top_code}."
                )
            )
        lines.extend(_render_drop_row(language, drop) for drop in drops[:10])
        lines.append(
            (
                "الترتيب وفق الهدف المحدد هو نتيجة سيناريو محدود، وليس إثباتًا بأن الحذف بلا "
                "أثر أكاديمي أو أنه مسموح تسجيلًا."
            )
            if language == "Arabic"
            else (
                "The objective-specific rank is a bounded scenario result, not proof that a drop "
                "has no academic consequence or is permitted."
            )
        )
    elif status == "NO_REGISTERED_CURRENT_COURSES":
        lines.append(
            "لا يوجد خط أساس لجدول مسجّل فعليًا يمكن ترتيب حذف مقرراته."
            if language == "Arabic"
            else "There is no actually registered timetable baseline whose courses can be ranked for dropping."
        )
    elif status == "BASELINE_REVIEW_REQUIRED":
        lines.append(
            "توقّف الترتيب لأن بيانات الجدول تجمع مصادر تسجيل وتخطيط تحتاج إلى مراجعة."
            if language == "Arabic"
            else "Ranking stopped because the timetable combines registration and planning sources that require review."
        )
    else:
        lines.append(
            "تعذّر تحديد ترتيب موثوق لأثر حذف المقررات في السيناريوهات المقيمة."
            if language == "Arabic"
            else "A reliable course-drop impact ranking was not determinable for the evaluated scenarios."
        )
    excluded = _mapping_rows(row.get("excluded_courses"))
    if excluded:
        lines.append(
            "مقررات لم تدخل المقارنة:"
            if language == "Arabic"
            else "Courses not included in the comparison:"
        )
        for item in excluded[:10]:
            code = _text(item.get("course_code")).upper()
            if not code:
                continue
            reason_code = _text(item.get("reason_code")).upper()
            if reason_code == "NOT_IN_CURRENT_TIMETABLE":
                explanation = (
                    "لم يوجد في خط أساس الجدول المسجّل فعليًا، لذلك لم يُقيّم كسيناريو حذف"
                    if language == "Arabic"
                    else (
                        "was not found in the actually registered timetable baseline, so it "
                        "was not evaluated as a drop scenario"
                    )
                )
            else:
                explanation = (
                    "لم يكتمل له سيناريو تخرج قابل للتحديد، لذلك لم يدخل الترتيب"
                    if language == "Arabic"
                    else (
                        "did not have a determinable completed graduation scenario, so it was "
                        "not included in the ranking"
                    )
                )
            lines.append(f"- {code} — {explanation}.")
    if status != "RANKING_AVAILABLE":
        lines.insert(0, _bounded_negative_line(language))
    lines.extend(_compound_context_lines(language, row))
    search = _mapping(row.get("search"))
    evaluated = _number(search.get("drop_scenarios_evaluated"))
    determinable = _number(search.get("determinable_scenarios"))
    if evaluated or determinable:
        lines.append(
            f"قُيّم {evaluated or '0'} سيناريو حذف، وكان {determinable or '0'} منها قابلًا للتحديد."
            if language == "Arabic"
            else f"Evaluated {evaluated or '0'} drop scenario(s); {determinable or '0'} were determinable."
        )
    lines.extend(_compound_read_only_footer(language))
    return "\n".join(lines)


def _schedule_metrics_parts(language: str, value: Any, *, prefix: str) -> list[str]:
    metrics = _mapping(value)
    parts: list[str] = []
    days = _number(metrics.get("days_on_campus"))
    span = _number(metrics.get("total_daily_span_minutes"))
    if days:
        parts.append(
            f"{prefix} أيام الحضور: {days}"
            if language == "Arabic"
            else f"{prefix} campus days: {days}"
        )
    if span:
        parts.append(
            f"{prefix} مجموع الامتداد اليومي: {span} دقيقة"
            if language == "Arabic"
            else f"{prefix} total daily span: {span} minutes"
        )
    return parts


def _replacement_line(language: str, replacement: dict[str, Any]) -> str:
    removed = _mapping(replacement.get("remove_course"))
    added = _mapping(replacement.get("add_course"))
    removed_label = _course_label(removed) or "?"
    added_label = _course_label(added) or "?"
    details: list[str] = []
    removed_credits = _number(removed.get("credit_hours"))
    added_credits = _number(added.get("credit_hours"))
    if removed_credits or added_credits:
        details.append(
            f"الساعات: {removed_credits or '?'} ← {added_credits or '?'}"
            if language == "Arabic"
            else f"credits: {removed_credits or '?'} → {added_credits or '?'}"
        )
    academic = _mapping(replacement.get("academic_improvement"))
    details.extend(_graduation_parts(language, academic))
    timetable = _mapping(replacement.get("timetable"))
    options = _mapping_rows(timetable.get("certified_options"))
    if options:
        first = options[0]
        option_credits = _number(first.get("credit_hours"))
        section_labels = [
            f"{_text(section.get('course_code')).upper()}-{_text(section.get('section')).upper()}"
            for section in _mapping_rows(first.get("complete_sections"))
            if _text(section.get("course_code")) and _text(section.get("section"))
        ]
        if option_credits:
            details.append(
                f"ساعات خيار الجدول: {option_credits}"
                if language == "Arabic"
                else f"timetable-option credits: {option_credits}"
            )
        if section_labels:
            details.append(
                "شُعب الخيار الكامل: " + "، ".join(section_labels)
                if language == "Arabic"
                else "complete-option sections: " + ", ".join(section_labels)
            )
    separator = "؛ " if language == "Arabic" else "; "
    transition = (
        f"{removed_label} ← {added_label}"
        if language == "Arabic"
        else f"{removed_label} → {added_label}"
    )
    return f"- {transition}" + (f" — {separator.join(details)}" if details else "")


def _schedule_improvement_line(language: str, schedule: dict[str, Any]) -> str:
    details: list[str] = []
    rank = _number(schedule.get("rank"))
    credits = _number(schedule.get("credit_hours"))
    if rank:
        details.append(f"الترتيب: {rank}" if language == "Arabic" else f"rank: {rank}")
    if credits:
        details.append(f"{credits} ساعات معتمدة" if language == "Arabic" else f"{credits} credits")
    changes: list[str] = []
    for change in _mapping_rows(schedule.get("changed_sections")):
        code = _text(change.get("course_code")).upper()
        old = _string_values(change.get("from_sections"), upper=True)
        new = _string_values(change.get("to_sections"), upper=True)
        if code:
            changes.append(f"{code}: {','.join(old) or '?'} → {','.join(new) or '?'}")
    if changes:
        details.append(
            "تغييرات الشُعب: " + "، ".join(changes)
            if language == "Arabic"
            else "section changes: " + ", ".join(changes)
        )
    details.extend(
        _schedule_metrics_parts(
            language,
            schedule.get("before"),
            prefix="قبل التغيير" if language == "Arabic" else "before",
        )
    )
    details.extend(
        _schedule_metrics_parts(
            language,
            schedule.get("after"),
            prefix="بعد التغيير" if language == "Arabic" else "after",
        )
    )
    improvement = _mapping(schedule.get("improvement"))
    saved_days = _number(improvement.get("campus_days_saved"))
    saved_span = _number(improvement.get("daily_span_minutes_saved"))
    if saved_days:
        details.append(
            f"أيام حضور موفرة: {saved_days}"
            if language == "Arabic"
            else f"campus days saved: {saved_days}"
        )
    if saved_span:
        details.append(
            f"دقائق امتداد يومي موفرة: {saved_span}"
            if language == "Arabic"
            else f"daily-span minutes saved: {saved_span}"
        )
    separator = "؛ " if language == "Arabic" else "; "
    return "- " + separator.join(details)


def render_improve_current_timetable(language: str, row: dict[str, Any]) -> str:
    """Render typed timetable improvements and bounded negative outcomes."""

    lines: list[str] = []
    if not row.get("ok"):
        lines.extend(
            [
                _bounded_negative_line(language),
                (
                    "تعذّر تقييم تحسين الجدول من الأدلة الموثقة المتاحة."
                    if language == "Arabic"
                    else "A timetable improvement could not be evaluated from the available verified evidence."
                ),
            ]
        )
        return "\n".join([*lines, *_compound_read_only_footer(language)])

    status = _text(row.get("status")).upper()
    if status == "IMPROVEMENTS_FOUND":
        lines.append(
            "وُجد تحسين موثّق ضمن البحث المحدود:"
            if language == "Arabic"
            else "A verified improvement was found within the bounded search:"
        )
        recommended = _mapping(row.get("recommended_change"))
        kind = _text(recommended.get("kind")).upper()
        if kind == "COURSE_REPLACEMENT":
            lines.append(
                "التغيير الموصى به في النتيجة: استبدال مقرر."
                if language == "Arabic"
                else "Recommended result change: course replacement."
            )
        elif kind == "SECTION_REARRANGEMENT":
            lines.append(
                "التغيير الموصى به في النتيجة: إعادة ترتيب الشُعب."
                if language == "Arabic"
                else "Recommended result change: section rearrangement."
            )
        replacements = _mapping_rows(row.get("graduation_improvements"))
        recommended_replacement = _mapping(recommended.get("replacement"))
        recommended_replacement_key = (
            _text(
                _mapping(recommended_replacement.get("remove_course")).get("course_code")
            ).upper(),
            _text(_mapping(recommended_replacement.get("add_course")).get("course_code")).upper(),
        )
        if recommended_replacement and not any(
            (
                _text(_mapping(item.get("remove_course")).get("course_code")).upper(),
                _text(_mapping(item.get("add_course")).get("course_code")).upper(),
            )
            == recommended_replacement_key
            for item in replacements
        ):
            replacements.insert(0, recommended_replacement)
        if replacements:
            lines.append(
                "بدائل المقررات ذات التحسن الأكاديمي المثبت:"
                if language == "Arabic"
                else "Course replacements with verified academic improvement:"
            )
            lines.extend(_replacement_line(language, item) for item in replacements[:10])
        schedules = _mapping_rows(row.get("schedule_quality_improvements"))
        recommended_schedule = _mapping(recommended.get("schedule"))
        if recommended_schedule and recommended_schedule not in schedules:
            schedules.insert(0, recommended_schedule)
        if schedules:
            lines.append(
                "تحسينات جودة الجدول المثبتة:"
                if language == "Arabic"
                else "Verified schedule-quality improvements:"
            )
            lines.extend(_schedule_improvement_line(language, item) for item in schedules[:3])
    elif status == "NO_VERIFIED_IMPROVEMENT_IN_BOUNDED_SEARCH":
        lines.append(
            (
                "لم يجد البحث المحدود تحسينًا موثقًا؛ ولا تعني هذه النتيجة عدم وجود "
                "أي تحسين خارج الفروع والتركيبات التي جرى فحصها."
            )
            if language == "Arabic"
            else (
                "The bounded search found no verified improvement; this does not mean no "
                "improvement exists outside the branches and combinations evaluated."
            )
        )
    elif status == "NO_REGISTERED_CURRENT_TIMETABLE":
        lines.append(
            "لا يوجد جدول مسجّل فعليًا يمكن استخدامه خط أساس للتحسين."
            if language == "Arabic"
            else "There is no actually registered current timetable to use as an improvement baseline."
        )
    elif status == "BASELINE_REVIEW_REQUIRED":
        reason_code = _text(row.get("reason_code")).upper()
        if reason_code == "REGISTERED_SECTION_MAPPING_INCOMPLETE":
            issue_codes = [
                _text(item.get("course_code")).upper()
                for item in _mapping_rows(row.get("baseline_mapping_issues"))
                if _text(item.get("course_code"))
            ]
            suffix = (" " + "، ".join(issue_codes)) if issue_codes else ""
            lines.append(
                (
                    "توقف البحث لأن بيانات الشعبة أو أوقات اللقاء الدقيقة غير مكتملة "
                    f"لبعض مقررات الجدول المسجّل:{suffix}."
                )
                if language == "Arabic"
                else (
                    "The search stopped because exact section or meeting facts are incomplete "
                    "for registered timetable course(s)"
                    + ((": " + ", ".join(issue_codes)) if issue_codes else "")
                    + "."
                )
            )
        else:
            lines.append(
                "توقف البحث لأن خط الأساس يجمع مصادر تسجيل وتخطيط تحتاج إلى مراجعة."
                if language == "Arabic"
                else "The search stopped because the baseline combines registration and planning sources requiring review."
            )
    elif status == "CONSTRAINTS_UNSATISFIED":
        constraints = _mapping(row.get("constraints"))
        effective_cap = _number(constraints.get("effective_max_credits"))
        baseline = _mapping(row.get("baseline"))
        baseline_credits = _number(baseline.get("credit_hours"))
        lines.append(
            "لم تُستوفَ قيود البحث، لذلك لم يُخفّض النظام الساعات أو يحذف مقررًا تلقائيًا."
            if language == "Arabic"
            else "The search constraints were not satisfied, so the system did not reduce credits or remove a course automatically."
        )
        if baseline_credits or effective_cap:
            lines.append(
                f"ساعات خط الأساس: {baseline_credits or '?'}؛ الحد الفعلي: {effective_cap or '?'}."
                if language == "Arabic"
                else f"Baseline credits: {baseline_credits or '?'}; effective limit: {effective_cap or '?'}."
            )
    elif status == "NO_SEARCH_BRANCH_ENABLED":
        lines.append(
            "لم يُفعّل الطلب أي فرع من فروع بحث تحسين الجدول."
            if language == "Arabic"
            else "The request enabled no timetable-improvement search branch."
        )
    else:
        lines.append(
            "تعذّر تحديد تحسين موثوق للجدول من الأدلة المسجلة."
            if language == "Arabic"
            else "A reliable timetable improvement was not determinable from the recorded evidence."
        )
    if status != "IMPROVEMENTS_FOUND":
        lines.insert(0, _bounded_negative_line(language))
    lines.extend(_compound_context_lines(language, row))
    search = _mapping(row.get("search"))
    if search.get("bounded") is True:
        lines.append(
            "كان البحث محدودًا وغير شامل لكل البدائل الممكنة."
            if language == "Arabic"
            else "The search was bounded and did not exhaust every possible alternative."
        )
    lines.extend(_compound_read_only_footer(language))
    return "\n".join(lines)


__all__ = [
    "render_course_prerequisites",
    "render_improve_current_timetable",
    "render_lookup_course",
    "render_plan_by_term",
    "render_rank_current_course_drop_impact",
    "render_recommend_feasible_course_addition",
]
