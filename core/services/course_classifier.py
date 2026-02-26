from core.services.student_helpers import normalize_code as global_normalize_code


def classify_courses(study_plan_courses: list[dict], timetable_courses: set[str]) -> dict:
    passed: list[str] = []
    studying: list[str] = []
    not_taken: list[str] = []

    normalized_timetable = {global_normalize_code(code) for code in timetable_courses}

    passing_letter_grades = {
        "A",
        "A+",
        "B",
        "B+",
        "C",
        "C+",
        "D",
        "D+",
        "أ",
        "أ+",
        "ب",
        "ب+",
        "ج",
        "ج+",
        "د",
        "د+",
    }

    for course in study_plan_courses:
        dept = course["dept"]
        number = course["no"]
        course_code = global_normalize_code(f"{dept} {number}")

        marks = course["marks"].strip()
        letter = course["letter"].strip().upper()
        passed_flag = False

        if letter == "TRS":
            passed_flag = True
        else:
            try:
                numeric_marks = float(marks)
                if numeric_marks >= 60:
                    passed_flag = True
            except ValueError:
                if letter in passing_letter_grades:
                    passed_flag = True

        if passed_flag:
            passed.append(course_code)
        elif course_code in normalized_timetable:
            studying.append(course_code)
        else:
            not_taken.append(course_code)

    return {"passed": passed, "studying": studying, "not_taken": not_taken}
