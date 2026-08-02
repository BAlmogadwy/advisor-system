from core.models import Prerequisite, Student, StudentCourse


def normalize_code(code: object | None) -> str:
    if code is None:
        return ""
    s = str(code)
    s = s.replace("\u00a0", " ")
    s = s.strip().upper()
    s = s.replace(" ", "")
    return s


def is_elective_slot(requirement_type: object | None) -> bool:
    """Whether a programme-requirement row is an elective PLACEHOLDER.

    THE answer, in the module both the student screens and the adviser capabilities
    already import, because there were two and they disagreed on seven real courses.

    The rule is the DECLARED `ProgrammeRequirement.type`, never the shape of the
    code. `student_unlock` used to match a `GS`/`GSE`/`FE` prefix first and consult
    the type second, which caught GS101, GS103, GS104, GS111, GS112, GS151 and
    GS152 — Islamic Studies, Arabic Language Skills, University Life Skills and
    Computer Skills. Every one of them is declared `Mandatory`, and every one was
    shown to students as a "choose one with your adviser" slot and counted in no
    progress bucket at all (issue #55).

    Code shape cannot work here for the reason the adviser side already documented:
    FE1 and CS1 look nothing alike, so a pattern that covers today's families misses
    tomorrow's — and, as it turned out, wrongly claims some of today's.
    """
    return "ELECTIVE" in str(requirement_type or "").upper()


def get_student_program(student_id: int | str) -> str | None:
    val = Student.objects.filter(student_id=student_id).values_list("program", flat=True).first()
    return val if val else None


def get_all_programs() -> list[str]:
    return list(
        Student.objects.exclude(program__isnull=True)  # type: ignore[arg-type]
        .exclude(program="")
        .values_list("program", flat=True)
        .distinct()
    )


def get_prerequisites(course_code: str, program: str) -> list[str]:
    course_code_n = normalize_code(course_code)
    program_n = str(program).strip().upper()
    # Filter by normalized course_code at DB level to avoid full table scan.
    # Data is typically stored normalized (uppercase, no spaces).
    rows = Prerequisite.objects.filter(
        program=program_n,
        course_code=course_code_n,
    ).values_list("prerequisite_course_code", flat=True)

    prereqs: list[str] = []
    for cell in rows:
        if cell is None:
            continue
        for code in str(cell).split(","):
            c = normalize_code(code)
            if c:
                prereqs.append(c)
    return prereqs


def get_prerequisites_visualizer_style(course_code: str, program: str) -> list[str]:
    rows = Prerequisite.objects.filter(
        course_code=course_code,
        program=program,
    ).values_list("prerequisite_course_code", flat=True)
    prereqs: list[str] = []
    for cell in rows:
        if cell is None:
            continue
        for c in str(cell).split(","):
            code = c.strip().upper()
            if code:
                prereqs.append(code)
    return prereqs


def get_student_passed_and_studying(student_id: int | str) -> tuple[set[str], set[str]]:
    rows = (
        StudentCourse.objects.filter(
            student_id=student_id,
        )
        .select_related("course")
        .values_list("course__course_code", "status")
    )

    passed: set[str] = set()
    studying: set[str] = set()
    for code, status in rows:
        c = normalize_code(code)
        if status == "passed":
            passed.add(c)
        elif status == "studying":
            studying.add(c)
    return passed, studying
