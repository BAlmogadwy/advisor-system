import re
from collections.abc import Iterable
from dataclasses import dataclass

from core.models import ElectiveCourse, ProgrammeRequirement, Student
from core.services.student_helpers import (
    get_all_programs,
    get_prerequisites,
    get_student_passed_and_studying,
    normalize_code,
)

_HOUR_PREREQ_RE = re.compile(r"^(\d+)\s*\(?\s*HOURS?\s*\)?$", re.IGNORECASE)


def split_hour_prereqs(prereqs: list[str]) -> tuple[list[str], int]:
    """Split a prerequisite list into real course codes and a credit-hour gate.

    The curriculum encodes "you need N credit hours" as a pseudo-prerequisite such
    as ``146(HOURS)``. It is NOT a course code: tested as one it can never be
    satisfied, which silently locks every capstone forever.
    """
    courses: list[str] = []
    required_hours = 0
    for p in prereqs:
        m = _HOUR_PREREQ_RE.match(str(p).strip())
        if m:
            required_hours = max(required_hours, int(m.group(1)))
        else:
            courses.append(p)
    return courses, required_hours


def effective_credits(earned: int, registered: int, *, strict_passed_only: bool = False) -> int:
    """Credits that count toward an hour gate under the chosen mode.

    THE single definition of relaxed vs strict for hour gates. Relaxed counts
    currently-registered credits as if they will be earned; strict counts only
    what is already earned. Both the eligibility report and the batch
    recommender consume this — a second inline copy of the rule is how the two
    screens once came to disagree about who may take a capstone.
    """
    return int(earned or 0) if strict_passed_only else int(earned or 0) + int(registered or 0)


def prereq_satisfied(
    prereq: str,
    passed: set[str],
    studying: set[str],
    *,
    strict_passed_only: bool = False,
) -> bool:
    """Is a single course prerequisite met under the chosen mode?

    THE single definition of relaxed vs strict for course prerequisites:
    relaxed accepts a course currently being studied; strict demands a pass.
    """
    return prereq in passed or (not strict_passed_only and prereq in studying)


@dataclass(frozen=True)
class PrerequisiteOutcome:
    """Whether one course's prerequisites are met, and precisely what is not."""

    met: bool
    missing_courses: list[str]
    required_hours: int
    effective_hours: int
    hours_met: bool

    @property
    def missing(self) -> list[str]:
        """What to SHOW a user: unmet course codes, plus the hour gate if short.

        The hour token is re-emitted in its curriculum form (``90(HOURS)``) only
        when the gate is genuinely unmet, so a display list never contains a
        pseudo-code that the student has in fact satisfied.
        """
        out = list(self.missing_courses)
        if not self.hours_met:
            out.append(f"{self.required_hours}(HOURS)")
        return out


def evaluate_prerequisites(
    raw_prereqs: Iterable[str],
    passed: set[str],
    studying: set[str],
    *,
    strict_passed_only: bool = False,
    earned_credits: int = 0,
    registered_credits: int = 0,
) -> PrerequisiteOutcome:
    """THE prerequisite check. Every screen consumes this one.

    The curriculum encodes "you need N credit hours" as a pseudo-prerequisite
    such as ``90(HOURS)``. It is NOT a course code: compared against a student's
    course list it can never match, so any caller that skips the split marks
    every capstone permanently blocked — measured at 68 live students across 13
    courses, every one of whom had actually met the gate. That is why the split
    lives inside this function rather than being each caller's job to remember.

    Deliberately PURE: credits are passed in, not queried. Batch callers
    (recommender_batch, high_priority_missing) pre-load them for hundreds of
    students at once, and a helper that queried per course would reintroduce the
    N+1 they exist to avoid. ``hour_gate`` is the single-student wrapper that
    does the lookup.
    """
    courses, required = split_hour_prereqs([str(p) for p in raw_prereqs])
    missing_courses = [
        c
        for c in courses
        if not prereq_satisfied(c, passed, studying, strict_passed_only=strict_passed_only)
    ]
    effective = effective_credits(
        earned_credits, registered_credits, strict_passed_only=strict_passed_only
    )
    hours_met = required <= 0 or effective >= required
    return PrerequisiteOutcome(
        met=not missing_courses and hours_met,
        missing_courses=missing_courses,
        required_hours=required,
        effective_hours=effective,
        hours_met=hours_met,
    )


def hour_gate(
    student_id: int | str,
    required_hours: int,
    *,
    strict_passed_only: bool = False,
    registered_credits_override: int | None = None,
) -> dict[str, object]:
    """Evaluate a credit-hour gate for one student.

    Mirrors the rule already used by course_eligibility: currently-registered
    credits count toward the gate unless ``strict_passed_only``.
    """
    row = (
        Student.objects.filter(student_id=student_id)
        .values_list("total_earned_credits", "current_registered_credits")
        .first()
    )
    earned, current = (row[0] or 0, row[1] or 0) if row else (0, 0)
    if registered_credits_override is not None:
        current = max(0, int(registered_credits_override))
    effective = effective_credits(earned, current, strict_passed_only=strict_passed_only)
    return {
        "required": int(required_hours),
        "earned": int(earned),
        "registered": int(current),
        "effective": int(effective),
        "met": bool(required_hours <= 0 or effective >= required_hours),
        "remaining": max(0, int(required_hours) - int(effective)),
    }


def _course_exists_in_program(course_code: str, program: str) -> bool:
    code_n = normalize_code(course_code)
    for row in ProgrammeRequirement.objects.filter(program=program).values_list(
        "course_code", flat=True
    ):
        if normalize_code(row) == code_n:
            return True
    # Also check elective catalogue
    return ElectiveCourse.objects.filter(programme=program, course_code__iexact=code_n).exists()


def _get_elective_prerequisites(course_code: str, program: str) -> list[str]:
    """Get prerequisites from ElectiveCourse catalogue if available."""
    code_n = normalize_code(course_code)
    ec = ElectiveCourse.objects.filter(programme=program, course_code__iexact=code_n).first()
    if ec and ec.prerequisites_csv:
        return [normalize_code(p) for p in ec.prerequisites_csv.split(",") if p.strip()]
    return []


def _get_filtered_students(
    section: str | None, program: str, join_year_prefixes: list[str] | None
) -> list[int]:
    qs = Student.objects.filter(program=program)
    if section:
        qs = qs.filter(section=section)
    if join_year_prefixes:
        from django.db.models import Q

        prefix_q = Q()
        for p in join_year_prefixes:
            if not p or not p.isdigit():
                continue
            low = int(p) * (10 ** (7 - len(p)))
            high = (int(p) + 1) * (10 ** (7 - len(p)))
            prefix_q |= Q(student_id__gte=low, student_id__lt=high)
        if prefix_q:
            qs = qs.filter(prefix_q)
    return list(qs.order_by("student_id").values_list("student_id", flat=True))


def build_course_eligibility_report(
    course_code: str,
    section: str | None = None,
    program: str | None = None,
    join_year_prefixes: list[str] | None = None,
    strict_passed_only: bool = False,
) -> dict:
    code = normalize_code(course_code)
    programs = [program] if program else get_all_programs()

    per_program: list[dict] = []
    total_students = 0
    total_eligible = 0

    for prog in programs:
        if not prog:
            continue
        if not _course_exists_in_program(code, prog):
            continue

        students = _get_filtered_students(
            section=section, program=prog, join_year_prefixes=join_year_prefixes
        )
        eligible_ids: list[int] = []
        blocked_samples: list[dict] = []
        missing_counter: dict[str, int] = {}
        prereqs = get_prerequisites(code, prog)
        if not prereqs:
            prereqs = _get_elective_prerequisites(code, prog)

        # Credits for the whole cohort in ONE query. Fetching them per student
        # inside the loop is a query per student for a figure that never varies
        # within the request.
        credits_by_student: dict[int, tuple[int, int]] = {
            row[0]: (row[1] or 0, row[2] or 0)
            for row in Student.objects.filter(student_id__in=students).values_list(
                "student_id", "total_earned_credits", "current_registered_credits"
            )
        }

        for sid in students:
            passed, studying = get_student_passed_and_studying(sid)
            if code in passed or code in studying:
                if len(blocked_samples) < 10:
                    blocked_samples.append(
                        {
                            "student_id": sid,
                            "reason": "already_taken_or_studying",
                            "missing_prerequisites": [],
                        }
                    )
                continue

            earned, current = credits_by_student.get(sid, (0, 0))
            outcome = evaluate_prerequisites(
                prereqs,
                passed,
                studying,
                strict_passed_only=strict_passed_only,
                earned_credits=earned,
                registered_credits=current,
            )
            missing = outcome.missing
            ok = outcome.met
            if ok:
                eligible_ids.append(sid)
            else:
                for m in missing:
                    missing_counter[m] = missing_counter.get(m, 0) + 1
                if len(blocked_samples) < 10:
                    blocked_samples.append(
                        {
                            "student_id": sid,
                            "reason": "missing_prerequisites",
                            "missing_prerequisites": missing,
                        }
                    )

        total_students += len(students)
        total_eligible += len(eligible_ids)
        blocked_count = max(0, len(students) - len(eligible_ids))
        blocked_ratio = (blocked_count / len(students)) if students else 0.0
        top_missing = sorted(missing_counter.items(), key=lambda x: (-x[1], x[0]))[:5]

        per_program.append(
            {
                "program": prog,
                "students": len(students),
                "eligible_count": len(eligible_ids),
                "eligible_student_ids": eligible_ids,
                "prerequisites": prereqs,
                "blocked_samples": blocked_samples,
                "blocked_count": blocked_count,
                "blocked_ratio": round(blocked_ratio, 4),
                "top_missing_prerequisites": [
                    {"course_code": k, "count": v} for k, v in top_missing
                ],
            }
        )

    return {
        "course_code": code,
        "strict_passed_only": strict_passed_only,
        "filters": {
            "section": section,
            "program": program,
            "join_year_prefixes": join_year_prefixes or [],
        },
        "total_students": total_students,
        "total_eligible": total_eligible,
        "per_program": per_program,
    }
