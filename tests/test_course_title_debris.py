"""Course names with the course's own number in front, where they came from and their cleaning.

Six course names carried debris in front: ")\\t203\\t CALCULUS I" (MATH203), "345\\t WEB
APPLICATION DEVELOPMENT II" (IS345), ... The Oracle plan parser was the source: it matched the
number between the Arabic and the English name as any number, so its lazy Arabic-name group
stopped at a number inside the Arabic name and the English name took the rest of the line. The
import stores that name in ``Course.description`` and in the plan row's ``course_name``.

The parser now matches the course's own number; the study-plan page parser, whose names a scrape
stores for new courses, drops the same debris; migration 0085 cleans what is stored.
"""

from __future__ import annotations

import importlib

import pytest

from core.models import Course, ProgrammeRequirement
from core.services.oracle_plan_parser import parse_oracle_plan
from core.services.student_parser import clean_course_title, parse_study_plan

TAB = chr(9)


def _oracle_line(ar_dept: str, number: str, dept: str, ar_name: str, en_name: str) -> str:
    """One course row shaped like the Oracle text export: type, the Arabic department and the
    number, hours, the English department, a sequence, then the Arabic name, the number again,
    the English name, the delivery, no prerequisite and no hour gate."""
    return (
        f"مقرر من الخطة {ar_dept} {number} 3 3 0 3 {dept} 1 {ar_name}{TAB}{number}{TAB} {en_name}"
        " تعليم حضوري لايوجد 0 \n"
    )


ORACLE_TEXT = (
    "الكلية: الحاسبات\n"
    "المستوى: Level الأول FIRST\n"
    # An Arabic name with a number in brackets, and one ending in a number: the two shapes behind
    # the six damaged names.
    + _oracle_line("رياض", "203", "MATH", "تفاضل وتكامل ( 1 )", "CALCULUS I")
    + _oracle_line("نظم", "345", "IS", "تطوير تطبيقات الويب 2", "WEB APPLICATION DEVELOPMENT II")
    + _oracle_line("حسب", "101", "CS", "مقدمة في البرمجة", "INTRODUCTION TO PROGRAMMING")
)


def _oracle_courses() -> dict[str, dict]:
    parsed = parse_oracle_plan(content=ORACLE_TEXT)
    return {
        course["code"]: course for level in parsed["levels"].values() for course in level["courses"]
    }


def test_the_oracle_parser_reads_the_english_name_after_the_courses_own_number() -> None:
    courses = _oracle_courses()

    assert {code: course["en_name"] for code, course in courses.items()} == {
        "MATH203": "CALCULUS I",
        "IS345": "WEB APPLICATION DEVELOPMENT II",
        "CS101": "INTRODUCTION TO PROGRAMMING",
    }
    # The Arabic name keeps the number that is part of it.
    assert courses["MATH203"]["ar_name"] == "تفاضل وتكامل ( 1 )"
    assert courses["IS345"]["ar_name"] == "تطوير تطبيقات الويب 2"


STUDY_PLAN_HTML = f"""
<table dir="rtl">
  <tr><th>FIRST LEVEL</th></tr>
  <tr><td>A</td><td>91</td><td>4</td><td>203</td><td>MATH</td><td>){TAB}203{TAB} CALCULUS I</td></tr>
  <tr><td>B</td><td>84</td><td>3</td><td>345</td><td>IS</td><td>345{TAB} WEB APPLICATION DEVELOPMENT II</td></tr>
  <tr><td></td><td></td><td>3</td><td>101</td><td>CS</td><td>INTRODUCTION TO PROGRAMMING</td></tr>
  <tr><td></td><td></td><td>3</td><td>350</td><td>CS</td><td>3D{TAB}GRAPHICS</td></tr>
  <tr><td></td><td></td><td>3</td><td>211</td><td>CS</td><td>101{TAB} NOT ITS OWN NUMBER</td></tr>
</table>
"""


def test_a_scraped_study_plan_never_stores_the_debris_as_a_name() -> None:
    names = {
        row["dept"] + row["no"]: row["description"] for row in parse_study_plan(STUDY_PLAN_HTML)
    }

    assert names == {
        "MATH203": "CALCULUS I",
        "IS345": "WEB APPLICATION DEVELOPMENT II",
        "CS101": "INTRODUCTION TO PROGRAMMING",
        # Only the course's OWN number in front is debris.
        "CS350": f"3D{TAB}GRAPHICS",
        "CS211": f"101{TAB} NOT ITS OWN NUMBER",
    }


def test_clean_course_title_leaves_a_name_that_is_only_the_number() -> None:
    assert clean_course_title(f"203{TAB}", "203") == "203"
    assert clean_course_title("  CALCULUS I ", "203") == "CALCULUS I"
    assert clean_course_title(None, "203") == ""


# ── Migration 0085 ────────────────────────────────────────────────────────────────────────────
DAMAGED = {
    "MATH203": f"){TAB}203{TAB} CALCULUS I",
    "MATH243": f"){TAB}243{TAB} LINEAR ALGEBRA I",
    "IS345": f"345{TAB} WEB APPLICATION DEVELOPMENT II",
    "COE224": f"){TAB}224{TAB} COMPUTER ORGANIZATION AND ARCHITECTURE I",
    "CYB388": f"388{TAB} GRADUATION PROJECT I",
    "COE321": f"){TAB}321{TAB} COMPUTER ORGANIZATION & ARCHITECTURE II",
}
CLEAN = {
    "MATH203": "CALCULUS I",
    "MATH243": "LINEAR ALGEBRA I",
    "IS345": "WEB APPLICATION DEVELOPMENT II",
    "COE224": "COMPUTER ORGANIZATION AND ARCHITECTURE I",
    "CYB388": "GRADUATION PROJECT I",
    "COE321": "COMPUTER ORGANIZATION & ARCHITECTURE II",
}
#: Names the migration must not touch: no debris, another number, no tab, a tab elsewhere.
UNTOUCHED = {
    "CS101": "INTRODUCTION TO PROGRAMMING",
    "CS211": f"101{TAB} NOT ITS OWN NUMBER",
    "CS350": f"3D{TAB}GRAPHICS",
    "CS360": "360 DEGREES",
    "CS370": f"SYSTEMS{TAB}370",
    "CS380": "",
}


def _migration():
    """The migration module, imported by path: its name starts with a digit."""
    return importlib.import_module("core.migrations.0085_clean_course_title_number_debris")


class _Apps:
    """``apps.get_model`` over the real models: the migration changes no schema."""

    @staticmethod
    def get_model(app_label: str, model_name: str):
        return {"Course": Course, "ProgrammeRequirement": ProgrammeRequirement}[model_name]


def _seed() -> None:
    for code, name in {**DAMAGED, **UNTOUCHED}.items():
        Course.objects.create(course_code=code, description=name, credit_hours=3)
        for program in ("CS", "COE"):
            ProgrammeRequirement.objects.create(
                program=program,
                course_code=code,
                course_name=name,
                programme_term=1,
                credit_hours=3,
            )


def _names() -> tuple[dict[str, str], dict[tuple[str, str], str]]:
    courses = dict(Course.objects.values_list("course_code", "description"))
    rows = {
        (program, code): name
        for program, code, name in ProgrammeRequirement.objects.values_list(
            "program", "course_code", "course_name"
        )
    }
    return courses, rows


@pytest.mark.django_db
def test_the_migration_cleans_exactly_the_debris_in_both_tables() -> None:
    _seed()

    _migration().forwards(_Apps, None)

    courses, rows = _names()
    assert courses == {**CLEAN, **UNTOUCHED}
    assert rows == {
        (program, code): name
        for program in ("CS", "COE")
        for code, name in {**CLEAN, **UNTOUCHED}.items()
    }


@pytest.mark.django_db
def test_the_migration_run_twice_changes_nothing_more() -> None:
    _seed()
    _migration().forwards(_Apps, None)
    once = _names()

    _migration().forwards(_Apps, None)

    assert _names() == once


@pytest.mark.django_db
def test_the_migration_is_a_no_op_on_empty_tables() -> None:
    _migration().forwards(_Apps, None)

    assert Course.objects.count() == 0
