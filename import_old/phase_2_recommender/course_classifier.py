# student_advisor_system/phase_2_recommender/course_classifier.py

from utils.student_helpers import normalize_code as global_normalize_code

def classify_courses(study_plan_courses, timetable_courses):
    """
    Classify all courses into passed, studying, or not_taken.

    Args:
        study_plan_courses: list of dicts from parse_study_plan()
        timetable_courses: set of "Dept No" strings from parse_timetable()

    Returns:
        dict: {
            'passed': list of course_codes,
            'studying': list of course_codes,
            'not_taken': list of course_codes
        }
    """
    passed = []
    studying = []
    not_taken = []

    normalized_timetable = {global_normalize_code(code) for code in timetable_courses}

    # Common passing letter grades (both EN and AR)
    passing_letter_grades = {
        "A", "A+", "B", "B+", "C", "C+", "D", "D+",
        "أ", "أ+", "ب", "ب+", "ج", "ج+", "د", "د+"
    }

    for course in study_plan_courses:
        dept = course["dept"]
        number = course["no"]
        course_code = global_normalize_code(f"{dept} {number}")

        marks = course["marks"].strip()
        letter = course["letter"].strip().upper()
        passed_flag = False

        # ✅ Check if course was passed via transfer credit
        if letter == 'TRS':
            passed_flag = True
        else:
            try:
                numeric_marks = float(marks)
                if numeric_marks >= 60:
                    passed_flag = True
            except ValueError:
                # No numeric mark → fall back to letter grade
                if letter in passing_letter_grades:
                    passed_flag = True

        if passed_flag:
            passed.append(course_code)
        elif course_code in normalized_timetable:
            studying.append(course_code)
        else:
            not_taken.append(course_code)

    return {
        "passed": passed,
        "studying": studying,
        "not_taken": not_taken
    }
