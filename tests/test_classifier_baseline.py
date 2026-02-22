from core.services.course_classifier import classify_courses


def test_classifier_baseline_rules() -> None:
    study_plan = [
        {"dept": "CS", "no": "101", "marks": "65", "letter": "F"},
        {"dept": "CS", "no": "102", "marks": "", "letter": "A"},
        {"dept": "CS", "no": "103", "marks": "", "letter": "F"},
        {"dept": "CS", "no": "104", "marks": "", "letter": "TRS"},
    ]
    timetable = {"CS103"}

    result = classify_courses(study_plan, timetable)

    assert "CS101" in result["passed"]
    assert "CS102" in result["passed"]
    assert "CS104" in result["passed"]
    assert "CS103" in result["studying"]
