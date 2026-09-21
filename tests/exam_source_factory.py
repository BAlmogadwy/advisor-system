"""Explicit scraped-registration setup for exam workflow test populations."""

from core.models import Course, Student, StudentTermSection, TermSection


def scraped_exam_registration(student, course, *, section_label=None, year="1448", term="1"):
    """Create one deliberate scraped link, never infer links from studying rows."""
    if not isinstance(student, Student):
        student = Student.objects.get(student_id=student)
    if isinstance(course, Course):
        code, name = course.course_code, course.description
    else:
        code, name = str(course), ""
    label = section_label if section_label is not None else f"{student.section}1"
    section, _ = TermSection.objects.get_or_create(
        course_key=code,
        section=label,
        scenario=None,
        defaults={
            "course_code": code,
            "course_number": "",
            "course_name": name,
            "source_tag": "scraper_timetable",
        },
    )
    link, _ = StudentTermSection.objects.get_or_create(
        student_id=student.student_id,
        academic_year=year,
        term=term,
        term_section=section,
        source="scraper_timetable",
    )
    return link
