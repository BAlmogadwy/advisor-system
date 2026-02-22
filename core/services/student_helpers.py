from core.models import Prerequisite, Student, StudentCourse


def normalize_code(code: object | None) -> str:
    if code is None:
        return ""
    s = str(code)
    s = s.replace("\u00A0", " ")
    s = s.strip().upper()
    s = s.replace(" ", "")
    return s


def get_student_program(student_id: int | str) -> str | None:
    val = Student.objects.filter(student_id=student_id).values_list("program", flat=True).first()
    return val if val else None


def get_all_programs() -> list[str]:
    return list(
        Student.objects.exclude(program__isnull=True)
        .exclude(program="")
        .values_list("program", flat=True)
        .distinct()
    )


def get_prerequisites(course_code: str, program: str) -> list[str]:
    course_code_n = normalize_code(course_code)
    program_n = str(program).strip().upper()
    rows = Prerequisite.objects.filter(
        program=program_n,
    ).values_list("course_code", "prerequisite_course_code")

    prereqs: list[str] = []
    for db_code, cell in rows:
        if normalize_code(db_code) != course_code_n:
            continue
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
    rows = StudentCourse.objects.filter(
        student_id=student_id,
    ).select_related("course").values_list("course__course_code", "status")

    passed: set[str] = set()
    studying: set[str] = set()
    for code, status in rows:
        c = normalize_code(code)
        if status == "passed":
            passed.add(c)
        elif status == "studying":
            studying.add(c)
    return passed, studying
