"""One course, explained to the student who asked about it.

ONE surface, not two. The recon proposed a "prerequisites" screen and an "elective
options" screen; `_exec_course_prerequisites` already showed they are one thing
branching on whether the code names a real course or a placeholder slot, and
splitting them would have meant two endpoints resolving the same code two ways.

**The Arabic is written here, before anything is assembled.** Every vocabulary on
this surface is closed — the status of a course, the status of a prerequisite, why
a course is blocked, whether a slot has published options — and every one of them
is translated from its CODE. Nothing derives a sentence from a solver string, a
requirement type, or an English label. The adviser shipped with every server error
rendering in English on an Arabic page, and a one-line rule in a design document
did not prevent that; writing the strings did.

**Nothing here says a student may register.** It says what a course *is*, what it
formally requires, and where they stand against it. `eligible_now` is a claim the
university acts on and it needs the canonical engine (issue #56); absence of a
blocking reason is not the same statement, and this module never converts one into
the other.
"""

from __future__ import annotations

from typing import Any

from core.services.student_helpers import get_prerequisites, is_elective_slot, normalize_code

#: What this course IS. Three values, not two: a code can name a real course, an
#: elective placeholder, or nothing in this student's programme at all — and that
#: third state is one `_exec_course_prerequisites` and `why_course_locked`
#: currently disagree about.
KIND_COURSE = "COURSE"
KIND_ELECTIVE_SLOT = "ELECTIVE_SLOT"
KIND_NOT_IN_PLAN = "NOT_IN_PLAN"

#: Where the student stands. `UNKNOWN` is a real answer and is not permission.
STATUS_AR: dict[str, str] = {
    "passed": "اجتزتَ هذا المقرر.",
    "studying": "تدرس هذا المقرر حاليًا، ويلزم اجتيازه.",
    "open_now": "استوفيتَ متطلباته السابقة.",
    "blocked": "لم تُستوفَ متطلباته السابقة بعد.",
    "unknown": "لا نستطيع تحديد وضعك في هذا المقرر من البيانات المتاحة.",
}

#: A prerequisite's own state, said in one word so a list of five reads at a glance.
PREREQ_STATUS_AR: dict[str, str] = {
    "passed": "مجتاز",
    "studying": "تدرسه حاليًا",
    # NOT «تستطيع تسجيله الآن». That is a personalised registration-permission
    # claim — the very thing this surface refuses to make — wearing the clothes of a
    # status badge. It says what is true of the PREREQUISITE, not what the student
    # may do about it.
    "open": "متطلباته السابقة مستوفاة",
    "blocked": "محجوب هو نفسه",
    "unknown": "غير معروف",
}

#: Why a course is blocked. FOUR members with DIFFERENT payloads — see
#: `student_unlock.py`. A flat `{code, text}` reason cannot carry three of them:
#: `MISSING_HOURS` has no course and is entirely numbers, `ASK_ADVISOR` has no
#: payload, and `UNKNOWN_PREREQ` names a code that is by definition not in the
#: plan, so it has no name and no chain to point at.
REASON_AR: dict[str, str] = {
    "MISSING_COURSE": "يتطلب اجتياز {course}.",
    "MISSING_HOURS": "يتطلب {required} ساعة معتمدة، ولديك {effective} — تبقّى {remaining}.",
    "UNKNOWN_PREREQ": "يذكر {course} كمتطلب سابق، وهو غير موجود في خطتك — راجع مرشدك.",
    "ASK_ADVISOR": "راجع مرشدك الأكاديمي لمعرفة سبب الحجب.",
}
REASON_AR_DEFAULT = "راجع مرشدك الأكاديمي لمعرفة سبب الحجب."

NOT_IN_PLAN_AR = "هذا المقرر ليس ضمن خطة برنامجك، فلا نستطيع تفسير وضعك فيه."
NO_PROGRAMME_AR = (
    "لا يوجد برنامج دراسي مسجَّل في ملفك، فلا توجد خطة يمكن الرجوع إليها. راجع القسم لتحديث بياناتك."
)


class CourseDetailUnavailable(Exception):
    """The question cannot be answered, and guessing would be worse than refusing.

    The planner's `PlannerUnavailable` sets the precedent: a student with no
    programme on file gets a sentence they can act on, not a report built from
    whichever programme happened to sort first.
    """


#: The two states that mean the student has settled a requirement themselves,
#: mapped to this surface's vocabulary. Anything else — `not_taken`, a status the
#: registrar invents next year, no row at all — is not a settlement and must fall
#: through to the ordinary answer rather than be guessed at.
SETTLED_STATUSES: dict[str, str] = {"passed": "passed", "studying": "studying"}


def _own_status(student_id: int, code: str) -> str:
    """This student's own recorded result for this code, or `""`.

    One query, and only asked where the answer changes what is said. `passed`
    outranks `studying` because a student who retook and passed has passed.
    """
    from core.models import StudentCourse

    found = set(
        StudentCourse.objects.filter(
            student_id=int(student_id), course__course_code__iexact=code
        ).values_list("status", flat=True)
    )
    for raw, value in SETTLED_STATUSES.items():
        if raw in found:
            return value
    return ""


def _reason_ar(reason: dict[str, Any], names: dict[str, str]) -> str:
    kind = str(reason.get("kind") or "")
    template = REASON_AR.get(kind)
    if template is None:
        return REASON_AR_DEFAULT
    code = normalize_code(reason.get("code") or "")
    return template.format(
        course=f"{code} {names.get(code, '')}".strip(),
        required=reason.get("required", ""),
        effective=reason.get("effective", ""),
        remaining=reason.get("remaining", ""),
    )


def build_course_detail(
    student_id: int,
    course_code: str,
    *,
    academic_year: str = "",
    term: str = "",
    report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Everything this surface shows, for one code, for one student.

    `report` is an already-built `build_unlock_report`, and it is a parameter for a
    measured reason: the report costs 118–158 queries, and the obvious journey —
    locked list, then "why?", then this — would pay it twice. A caller that already
    has one passes it along instead of re-deriving it.

    It is also built LAST, and only for a real course: `NOT_IN_PLAN` and every
    elective slot answer without it.
    """
    from core.models import ProgrammeRequirement, Student
    from core.services.elective_readiness import READY, slot_status, student_message
    from core.services.student_unlock import build_unlock_report

    code = normalize_code(course_code)
    if not code:
        raise CourseDetailUnavailable("لم يُحدَّد رمز المقرر.")

    program = str(
        Student.objects.filter(student_id=int(student_id)).values_list("program", flat=True).first()
        or ""
    ).strip()
    if not program:
        raise CourseDetailUnavailable(NO_PROGRAMME_AR)

    # The term is resolved ONCE, here, and echoed in every response. Three code
    # paths in this project pick a term three different ways and agree today only
    # because the live data is one combination; a surface that does not say which
    # term it answered for cannot be checked.
    if not academic_year or not term:
        from core.services.planner_drafts import planning_term

        default_year, default_term = planning_term()
        academic_year = academic_year or default_year
        term = term or default_term

    # CLASSIFY FIRST, then pay for the report — and only in the branch that reads
    # it. `build_unlock_report` costs 118-158 queries, and it used to run before
    # this row was even fetched, so `NOT_IN_PLAN` and every elective slot paid for
    # a report neither of them opens. The unmapped elective slot is not a rare
    # path: 31 of 38 declared slots are unmapped — 10 of the 12 in a programme that
    # has students — so the most common elective answer on the whole surface was
    # also the most expensive way of saying "nothing published yet": measured at 129
    # queries to return one sentence.
    row = (
        ProgrammeRequirement.objects.filter(program__iexact=program, course_code__iexact=code)
        .values("type", "course_name", "credit_hours")
        .first()
    )

    base: dict[str, Any] = {
        "course_code": code,
        "program": program,
        "academic_year": academic_year,
        "term": term,
    }

    if row is None:
        # The third `kind`. `_exec_course_prerequisites` answers "not found",
        # `why_course_locked` returns an error and `eligibility` passes over it in
        # silence; a student clicking a code from a friend's programme lands here.
        #
        # Deliberately NO global catalogue lookup for a name. A code valid in
        # another programme must stay NOT_IN_PLAN and stop there — resolving it
        # against the whole catalogue would turn a plan-scoped surface into a
        # course search, one step from answering about a programme this student is
        # not in.
        return {**base, "kind": KIND_NOT_IN_PLAN, "message_ar": NOT_IN_PLAN_AR}

    if is_elective_slot(row["type"]):
        # BEFORE the gate: has this student already settled the slot? The registrar
        # records a result against the PLACEHOLDER code — 26 students have `DS1`
        # passed — so a slot is not automatically an open question.
        #
        # Narrowing the placeholder set was not enough to fix this. Free and
        # University Electives stopped being slots, which answered 364 enrolments;
        # the ordering defect that produced them survived for the set that stayed,
        # because the type was read before the student was. A code can be a
        # placeholder AND completed, and the answer to someone who completed it is
        # not "the options have not been published yet".
        settled = _own_status(int(student_id), code)
        if settled:
            return {
                **base,
                "kind": KIND_ELECTIVE_SLOT,
                "course_name": str(row.get("course_name") or ""),
                "credit_hours": row.get("credit_hours") or 0,
                "your_status": settled,
                "status_ar": STATUS_AR.get(settled, STATUS_AR["unknown"]),
                # No `slot_status` call, and deliberately no options: "choose one of
                # these" is not something to say to a student who already did. It
                # also costs nothing to answer, which is the right shape — the
                # cheapest answer for the student with the least to decide.
                "mapping_ready": False,
                "message_ar": "",
                "options": [],
            }

        # The gate is a BACKEND answer, never a programme name in a template.
        # `slot_status` returns options ONLY when READY — a caller that received
        # them in any other state would be one `if` away from rendering the list
        # the gate exists to withhold.
        status, options, _problems = slot_status(program, code, academic_year, term)
        return {
            **base,
            "kind": KIND_ELECTIVE_SLOT,
            "course_name": str(row.get("course_name") or ""),
            "credit_hours": row.get("credit_hours") or 0,
            # A BOOLEAN, never the operational state name. `student_message`
            # deliberately says the same thing for every non-ready state so a
            # student cannot tell "nobody published this" from "somebody published
            # it wrongly" — and shipping `INVALID_MAPPING` in the payload alongside
            # it hands them exactly that, one view-source away. The detailed state
            # stays in `readiness()`, where an operator reads it.
            "mapping_ready": status == READY,
            "message_ar": student_message(status),
            "options": [
                {
                    "course_code": normalize_code(o.get("course_code") or ""),
                    "course_name": str(o.get("course_name") or ""),
                    "credit_hours": o.get("credit_hours") or 0,
                    "prerequisites": [
                        {"course_code": normalize_code(c)}
                        for c in str(o.get("prerequisites_csv") or "").split(",")
                        if c.strip()
                    ],
                }
                for o in options
            ],
        }

    # Only here. A real course is the one branch that reads the report, and a
    # caller that already built one still passes it in rather than paying twice.
    if report is None:
        report = build_unlock_report(int(student_id), int(academic_year), int(term)) or {}
    return _course_detail(code, program, row, report, base)


def _course_detail(
    code: str,
    program: str,
    row: dict[str, Any],
    report: dict[str, Any],
    base: dict[str, Any],
) -> dict[str, Any]:
    from core.services.eligibility import split_hour_prereqs
    from core.services.virtual_advisor import _course_names

    status = "unknown"
    reasons: list[dict[str, Any]] = []
    for bucket, value in (
        ("done", "passed"),
        ("in_progress", "studying"),
        ("open_courses", "open_now"),
        ("locked_courses", "blocked"),
    ):
        entry = next((c for c in report.get(bucket) or [] if c.get("code") == code), None)
        if entry is not None:
            status = value
            reasons = list(entry.get("reasons") or [])
            break

    prereq_codes, hours_gate = split_hour_prereqs(get_prerequisites(code, program))
    graph = report.get("graph") or {}
    status_of = graph.get("statusOf") or {}

    # Already computed by the report and thrown away by every caller: "if I pass
    # this, what opens?" is the question a student asks next.
    unlocks = sorted(
        {
            e["course_code"]
            for e in (graph.get("items") or [])
            if e.get("prerequisite_course_code") == code
        }
    )

    # AFTER `unlocks`, and including it. Looking the names up first meant every
    # downstream course rendered with an empty name — the list showed codes and
    # nothing else, and the test only asserted the codes, so it passed.
    names = _course_names(
        {code} | {normalize_code(c) for c in prereq_codes} | {normalize_code(u) for u in unlocks}
    )

    return {
        **base,
        "kind": KIND_COURSE,
        "course_name": names.get(code) or str(row.get("course_name") or ""),
        "credit_hours": row.get("credit_hours") or 0,
        "your_status": status,
        "status_ar": STATUS_AR.get(status, STATUS_AR["unknown"]),
        "prerequisites": [
            {
                "course_code": normalize_code(c),
                "course_name": names.get(normalize_code(c), ""),
                "student_status": str(status_of.get(normalize_code(c)) or "unknown"),
                "student_status_ar": PREREQ_STATUS_AR.get(
                    str(status_of.get(normalize_code(c)) or "unknown"),
                    PREREQ_STATUS_AR["unknown"],
                ),
            }
            for c in prereq_codes
        ],
        "credit_hours_required": hours_gate or 0,
        "reasons": [
            {"kind": str(r.get("kind") or ""), "text_ar": _reason_ar(r, names)} for r in reasons
        ],
        "unlocks": [{"course_code": u, "course_name": names.get(u, "")} for u in unlocks],
    }


__all__ = [
    "KIND_COURSE",
    "KIND_ELECTIVE_SLOT",
    "KIND_NOT_IN_PLAN",
    "CourseDetailUnavailable",
    "build_course_detail",
]
