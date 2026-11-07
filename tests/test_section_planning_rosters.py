"""Section Planning's second export: the students behind every section count.

- The plan export says how many sections a course needs; this one says which
  students that demand is made of, a row per student per course, named by
  student id. It is a roster, not a section assignment: no student is put in
  a numbered section.
- It is the same plan seen twice: the same scope, the same chosen section, the
  same department filter, and a course's Sections here is the Sections the
  plan export writes for it.
- A student registers a course once. Where one student's recommendations ask
  for the same course twice (directly and through an elective slot), the plan
  counts both seats — that is what sized the sections — and the roster names
  the student once; the Courses sheet shows both numbers.
- The page's own result never carries a student list: rosters are the export's
  alone.
"""

from __future__ import annotations

import io
import json

import pytest
from django.contrib.auth.models import Group, User
from django.test import Client

from core.authz import _rate_buckets
from core.models import (
    Course,
    ElectiveCourse,
    ElectiveTermMapping,
    ProgrammeRequirement,
    Student,
)
from core.services.rbac import (
    ROLE_ADVISOR,
    ROLE_GENERAL_ADVISOR,
    ensure_role_groups,
    ensure_scope_schema,
    set_user_scope,
)
from core.services.reporting import clear_aggregate_cache
from core.services.section_roster_export import ROSTER_HEADERS, build_roster_rows

pytestmark = pytest.mark.django_db

ENDPOINT = "/ops/section-planning/export-rosters/"


@pytest.fixture(autouse=True)
def _fresh_state():
    _rate_buckets.clear()
    clear_aggregate_cache()
    yield
    _rate_buckets.clear()
    clear_aggregate_cache()


def _login(client: Client, username: str, role: str = ROLE_GENERAL_ADVISOR) -> Client:
    ensure_role_groups()
    ensure_scope_schema()
    user, _ = User.objects.get_or_create(username=username)
    user.groups.clear()
    user.groups.add(Group.objects.get(name=role))
    set_user_scope(user.id, advisor_id="", departments="")
    client.force_login(user)
    return client


@pytest.fixture
def planner(client: Client) -> Client:
    return _login(client, "sp-roster")


def _course(code: str, *, credits: int = 3, external: bool = False) -> None:
    Course.objects.create(
        course_code=code,
        credit_hours=credits,
        is_external=external,
        department="GS" if external else "".join(c for c in code if c.isalpha()),
        description=f"{code} CATALOGUE",
    )


def _requirement(program: str, code: str, *, cap: int | None = None, credits: int = 3) -> None:
    ProgrammeRequirement.objects.create(
        program=program,
        course_code=code,
        course_name=f"{code} NAME",
        credit_hours=credits,
        type="Mandatory",
        programme_term=5,
        max_capacity=cap,
    )


def _students(program: str, first_id: int, count: int, *, section: str = "M") -> list[int]:
    ids = list(range(first_id, first_id + count))
    Student.objects.bulk_create(
        [
            Student(
                student_id=sid,
                registration_no=str(sid),
                name=f"STUDENT {sid}",
                program=program,
                section=section,
            )
            for sid in ids
        ]
    )
    return ids


def _recommend_each(monkeypatch: pytest.MonkeyPatch, by_student: dict[int, list[str]]) -> None:
    """Each student is recommended their own courses (read at call time)."""

    def single(student_ids, _program, _year, _term, **_kw):
        return {sid: list(by_student.get(sid, [])) for sid in student_ids}

    def multi(student_ids, _year, _term, **_kw):
        return {sid: list(by_student.get(sid, [])) for sid in student_ids}

    monkeypatch.setattr("core.services.reporting.batch_recommend", single)
    monkeypatch.setattr("core.services.reporting.batch_recommend_multi_program", multi)


def _post(client: Client, endpoint: str, body: dict):
    _rate_buckets.clear()  # these tests export more often than a person may
    return client.post(
        endpoint,
        json.dumps({"year": 1448, "semester": 1, **body}),
        content_type="application/json",
    )


def _workbook(client: Client, section: str, **body):
    """The roster workbook, read back as a CI machine reads it (no lxml there)."""
    from openpyxl import load_workbook

    response = _post(client, ENDPOINT, {"section": section, **body})
    assert response.status_code == 200, response.content
    content = b"".join(response.streaming_content)
    return response, load_workbook(io.BytesIO(content), data_only=False)


def _rows(sheet, header_row: int) -> list[dict]:
    headers = [cell.value for cell in sheet[header_row]]
    return [
        dict(zip(headers, [cell.value for cell in row], strict=False))
        for row in sheet.iter_rows(min_row=header_row + 1)
        if row[0].value is not None
    ]


def _roster_rows(client: Client, section: str, **body) -> list[dict]:
    return _rows(_workbook(client, section, **body)[1]["Rosters"], 2)


def _course_rows(client: Client, section: str, **body) -> list[dict]:
    return _rows(_workbook(client, section, **body)[1]["Courses"], 3)


# ── access ───────────────────────────────────────────────────────


def test_rosters_export_requires_login(client: Client) -> None:
    """Unauthenticated → 401, as the plan export is."""
    response = _post(client, ENDPOINT, {"section": "M"})
    assert response.status_code == 401


def test_rosters_export_requires_general_advisor(client: Client) -> None:
    """An advisor may not download the department's rosters."""
    _login(client, "sp-roster-advisor", ROLE_ADVISOR)
    response = _post(client, ENDPOINT, {"section": "M"})
    assert response.status_code == 403


def test_rosters_export_requires_a_section(planner: Client) -> None:
    """One section per plan: no section chosen, no file."""
    response = _post(planner, ENDPOINT, {})
    assert response.status_code == 400
    assert response.json()["code"] == "section_required"


def test_rosters_export_rejects_a_bad_section(planner: Client) -> None:
    response = _post(planner, ENDPOINT, {"section": "X"})
    assert response.status_code == 400
    assert response.json()["code"] == "section_invalid"


# ── the roster itself ────────────────────────────────────────────


def test_a_row_per_student_per_course(planner: Client, monkeypatch) -> None:
    """Every student of a course is named, once, with their id, name and programme."""
    _course("AI301")
    _course("AI302")
    _requirement("AI", "AI301")
    _requirement("AI", "AI302")
    ids = _students("AI", 7001, 3)
    _recommend_each(
        monkeypatch,
        {ids[0]: ["AI301", "AI302"], ids[1]: ["AI301"], ids[2]: ["AI302"]},
    )

    rows = _roster_rows(planner, "M", program="AI")

    by_course: dict[str, list[dict]] = {}
    for row in rows:
        by_course.setdefault(row["Course"], []).append(row)
    assert [r["Student ID"] for r in by_course["AI301"]] == [ids[0], ids[1]]
    assert [r["Student ID"] for r in by_course["AI302"]] == [ids[0], ids[2]]
    assert [r["Student Name"] for r in by_course["AI301"]] == [
        f"STUDENT {ids[0]}",
        f"STUDENT {ids[1]}",
    ]
    assert {r["Programme"] for r in rows} == {"AI"}
    assert {r["Course Name"] for r in rows} == {"AI301 NAME", "AI302 NAME"}


def test_student_id_is_written_as_a_number(planner: Client, monkeypatch) -> None:
    """Excel must sort and filter the id as a number, not as text."""
    _course("AI303")
    _requirement("AI", "AI303")
    ids = _students("AI", 7100, 1)
    _recommend_each(monkeypatch, {ids[0]: ["AI303"]})

    rows = _roster_rows(planner, "M", program="AI")

    assert [r["Student ID"] for r in rows] == [ids[0]]
    assert isinstance(rows[0]["Student ID"], int)


def test_only_the_chosen_section_is_rostered(planner: Client, monkeypatch) -> None:
    """A student of the other section is in neither this roster nor its counts."""
    _course("AI304")
    _requirement("AI", "AI304")
    male = _students("AI", 7200, 2, section="M")
    female = _students("AI", 7300, 3, section="F")
    _recommend_each(monkeypatch, {sid: ["AI304"] for sid in male + female})

    male_rows = _roster_rows(planner, "M", program="AI")
    female_rows = _roster_rows(planner, "F", program="AI")

    assert [r["Student ID"] for r in male_rows] == male
    assert [r["Student ID"] for r in female_rows] == female


def test_a_student_with_no_recorded_section_is_in_no_roster(planner: Client, monkeypatch) -> None:
    """Never assumed into a section: counted in the note, named in neither file."""
    _course("AI305")
    _requirement("AI", "AI305")
    known = _students("AI", 7400, 1, section="M")
    blank = _students("AI", 7500, 1, section="")
    _recommend_each(monkeypatch, {sid: ["AI305"] for sid in known + blank})

    _, workbook = _workbook(planner, "M", program="AI")
    rows = _rows(workbook["Rosters"], 2)

    assert [r["Student ID"] for r in rows] == known
    note = str(workbook["Courses"].cell(row=2, column=1).value)
    assert "1 student has no recorded section" in note


def test_the_roster_names_a_doubly_counted_student_once(planner: Client, monkeypatch) -> None:
    """One student, one seat in the roster — even where the plan counted two.

    A recommendation of a course and an elective slot that resolves to the
    same course both add a seat to the demand that sized the sections. The
    roster is who registers, so the student is named once, and the Courses
    sheet shows the two numbers side by side.
    """
    _course("AI306")
    _requirement("AI", "AI306")
    ids = _students("AI", 7600, 1)
    # The same course twice for the one student, as resolution can produce.
    _recommend_each(monkeypatch, {ids[0]: ["AI306", "AI306"]})

    _, workbook = _workbook(planner, "M", program="AI")
    roster = _rows(workbook["Rosters"], 2)
    courses = _rows(workbook["Courses"], 3)

    assert [r["Student ID"] for r in roster] == ids
    assert courses[0]["Students"] == 2  # the seat demand that sized the sections
    assert courses[0]["Listed"] == 1  # the students the roster names


def test_a_resolved_elective_is_rostered_under_its_real_course(
    planner: Client, monkeypatch
) -> None:
    """The slot's students are named under the course the slot became."""
    _course("AI480")
    ProgrammeRequirement.objects.create(
        program="AI",
        course_code="AI1",
        course_name="Program Elective",
        credit_hours=3,
        type="Program Elective",
        programme_term=8,
    )
    elective = ElectiveCourse.objects.create(
        programme="AI",
        course_code="AI480",
        course_name="AI480 ELECTIVE",
        credit_hours=3,
        prerequisites_csv="",
    )
    ElectiveTermMapping.objects.create(
        academic_year="1448", term=1, programme="AI", placeholder_code="AI1", elective=elective
    )
    ids = _students("AI", 7700, 2)
    _recommend_each(monkeypatch, {sid: ["AI1"] for sid in ids})

    rows = _roster_rows(planner, "M", program="AI")

    assert [(r["Course"], r["Student ID"]) for r in rows] == [("AI480", ids[0]), ("AI480", ids[1])]


# ── the same plan, seen twice ────────────────────────────────────


def test_sections_match_the_plan_export(planner: Client, monkeypatch) -> None:
    """A course's Sections is the number the plan export writes for it."""
    from openpyxl import load_workbook

    _course("AI307", credits=4)  # local 4-credit → 25 a section
    _requirement("AI", "AI307", credits=4)
    ids = _students("AI", 7800, 30)
    _recommend_each(monkeypatch, {sid: ["AI307"] for sid in ids})

    plan_response = _post(
        planner, "/ops/section-planning/export/", {"section": "M", "program": "AI"}
    )
    assert plan_response.status_code == 200
    plan_sheet = load_workbook(
        io.BytesIO(b"".join(plan_response.streaming_content)), data_only=False
    )["Sections"]
    plan_row = _rows(plan_sheet, 2)[0]

    _, workbook = _workbook(planner, "M", program="AI")
    courses = _rows(workbook["Courses"], 3)
    roster = _rows(workbook["Rosters"], 2)

    assert plan_row["Max/Section"] == 25
    assert courses[0]["Max/Section"] == 25
    assert courses[0]["Sections"] == 2  # ceil(30 / 25), as the plan export's formula gives
    assert courses[0]["Students"] == plan_row["Students"] == 30
    assert courses[0]["Listed"] == 30
    # Every roster row carries its course's sizing, so a filtered sheet still
    # says how many sections the rows in front of you are for.
    assert len(roster) == 30
    assert {r["Sections"] for r in roster} == {2}
    assert {r["Max/Section"] for r in roster} == {25}
    assert {r["Credits"] for r in roster} == {4}


def test_the_department_filter_scopes_the_roster(planner: Client, monkeypatch) -> None:
    """Filter CS and no AI student is named — the plan export's own filter."""
    _course("AI308")
    _course("CS308")
    _requirement("AI", "AI308")
    _requirement("AI", "CS308")
    ids = _students("AI", 7900, 2)
    _recommend_each(monkeypatch, {sid: ["AI308", "CS308"] for sid in ids})

    rows = _roster_rows(planner, "M", program="AI", dept_filter="CS")

    assert {r["Course"] for r in rows} == {"CS308"}
    assert [r["Student ID"] for r in rows] == ids


def test_multi_programme_pools_the_roster_once(planner: Client, monkeypatch) -> None:
    """Two programmes, one shared course: each student named once, with their programme."""
    _course("GS101", external=True)
    _requirement("AI", "GS101")
    _requirement("DS", "GS101")
    ai = _students("AI", 8000, 2)
    ds = _students("DS", 8100, 2)
    _recommend_each(monkeypatch, {sid: ["GS101"] for sid in ai + ds})

    rows = _roster_rows(planner, "M", program="AI,DS")

    assert [r["Student ID"] for r in rows] == ai + ds
    assert {r["Programme"] for r in rows if r["Student ID"] in ai} == {"AI"}
    assert {r["Programme"] for r in rows if r["Student ID"] in ds} == {"DS"}
    assert len(rows) == len({r["Student ID"] for r in rows})


def test_every_programme_mode_names_its_students(planner: Client, monkeypatch) -> None:
    """All programmes (no scope): the roster covers every programme's students."""
    _course("AI309")
    _course("DS309")
    _requirement("AI", "AI309")
    _requirement("DS", "DS309")
    ai = _students("AI", 8200, 2)
    ds = _students("DS", 8300, 1)
    _recommend_each(monkeypatch, {**{s: ["AI309"] for s in ai}, **{s: ["DS309"] for s in ds}})

    rows = _roster_rows(planner, "M")

    assert {(r["Course"], r["Student ID"]) for r in rows} == {
        ("AI309", ai[0]),
        ("AI309", ai[1]),
        ("DS309", ds[0]),
    }


# ── the file, and what it does not claim ─────────────────────────


def test_the_file_is_a_named_xlsx_download(planner: Client, monkeypatch) -> None:
    _course("AI310")
    _requirement("AI", "AI310")
    ids = _students("AI", 8400, 1)
    _recommend_each(monkeypatch, {ids[0]: ["AI310"]})

    response, workbook = _workbook(planner, "M", program="AI")

    assert "spreadsheet" in response["Content-Type"]
    assert "section_rosters_1448_1_AI_M.xlsx" in response["Content-Disposition"]
    assert workbook.sheetnames == ["Rosters", "Courses"]


def test_both_sheets_name_the_section_they_are_for(planner: Client, monkeypatch) -> None:
    """One section per plan: each sheet says which, so two files never mix up."""
    _course("AI314")
    _requirement("AI", "AI314")
    ids = _students("AI", 8700, 1, section="F")
    _recommend_each(monkeypatch, {ids[0]: ["AI314"]})

    _, workbook = _workbook(planner, "F", program="AI")

    for sheet in ("Rosters", "Courses"):
        title = str(workbook[sheet].cell(row=1, column=1).value)
        assert "Section: Female (F)" in title, f"{sheet}: {title}"
        assert "1448/1" in title, f"{sheet}: {title}"
    assert "Male" not in str(workbook["Rosters"].cell(row=1, column=1).value)


def test_a_course_with_no_name_of_its_own_takes_the_catalogue_name() -> None:
    """A row whose plan declares no name is still named, from the catalogue."""
    from openpyxl import load_workbook

    from core.services.section_roster_export import export_course_rosters_xlsx

    Student.objects.create(student_id=9100, name="NAMED", program="AI", section="M")
    path = export_course_rosters_xlsx(
        [{"department": "AI", "course_code": "AI402", "course_name": "", "student_ids": [9100]}],
        section_label="Male (M)",
        course_names={"AI402": "FROM CATALOGUE"},
    )
    workbook = load_workbook(path, data_only=False)

    assert [r["Course Name"] for r in _rows(workbook["Rosters"], 2)] == ["FROM CATALOGUE"]
    assert [r["Course Name"] for r in _rows(workbook["Courses"], 3)] == ["FROM CATALOGUE"]


def test_the_roster_claims_no_section_assignment(planner: Client, monkeypatch) -> None:
    """It says who registers a course, not which numbered section they sit in."""
    _course("AI311")
    _requirement("AI", "AI311")
    ids = _students("AI", 8500, 2)
    _recommend_each(monkeypatch, {sid: ["AI311"] for sid in ids})

    _, workbook = _workbook(planner, "M", program="AI")
    sheet = workbook["Rosters"]
    headers = [cell.value for cell in sheet[2]]

    assert "not a section assignment" in str(sheet.cell(row=1, column=1).value)
    # No column puts a student in one of the planned sections.
    assert "Section #" not in headers
    assert "Section No" not in headers


def test_an_empty_plan_makes_a_workbook_with_no_roster_rows(planner: Client) -> None:
    """No students, no rows — and no invented ones."""
    _, workbook = _workbook(planner, "M", program="AI")

    assert _rows(workbook["Rosters"], 2) == []
    assert _rows(workbook["Courses"], 3) == []


def test_the_page_result_carries_no_student_list(planner: Client, monkeypatch) -> None:
    """Generate answers the page, which shows counts: never a roster it does not show."""
    _course("AI312")
    _requirement("AI", "AI312")
    ids = _students("AI", 8600, 2)
    _recommend_each(monkeypatch, {sid: ["AI312"] for sid in ids})

    response = _post(planner, "/ops/section-planning/generate/", {"section": "M", "program": "AI"})

    assert response.status_code == 200
    body = response.content.decode("utf-8")
    assert "student_ids" not in body
    assert str(ids[0]) not in body


def test_a_name_that_looks_like_a_formula_stays_text(planner: Client, monkeypatch) -> None:
    """A registrar name beginning with = must not run when the file is opened.

    openpyxl writes a string starting with ``=`` as a FORMULA, so a scraped
    name such as ``=HYPERLINK(...)`` would be live in the department's
    workbook. Every string this export writes is data.
    """
    _course("AI315")
    _requirement("AI", "AI315")
    Student.objects.create(
        student_id=8800,
        registration_no="8800",
        name='=HYPERLINK("http://evil.test","OPEN")',
        program="AI",
        section="M",
    )
    _recommend_each(monkeypatch, {8800: ["AI315"]})

    _, workbook = _workbook(planner, "M", program="AI")
    sheet = workbook["Rosters"]
    name = sheet.cell(row=3, column=ROSTER_HEADERS.index("Student Name") + 1)

    assert name.value == '=HYPERLINK("http://evil.test","OPEN")'
    assert name.data_type == "s", "the name was written as a formula"
    # No cell anywhere in either sheet is a formula: this workbook has none.
    for name_of_sheet in ("Rosters", "Courses"):
        formulas = [
            cell.coordinate
            for row in workbook[name_of_sheet].iter_rows()
            for cell in row
            if cell.data_type == "f"
        ]
        assert formulas == [], f"{name_of_sheet}: {formulas}"


def test_the_export_is_throttled_like_the_plan_export(planner: Client, monkeypatch) -> None:
    """Five a window: the roster file is heavier than the plan's, not lighter."""
    _course("AI316")
    _requirement("AI", "AI316")
    ids = _students("AI", 8900, 1)
    _recommend_each(monkeypatch, {ids[0]: ["AI316"]})

    _rate_buckets.clear()
    codes = []
    for _ in range(6):
        # Not through _post: that clears the buckets for the other tests' sake.
        codes.append(
            planner.post(
                ENDPOINT,
                json.dumps({"year": 1448, "semester": 1, "section": "M", "program": "AI"}),
                content_type="application/json",
            ).status_code
        )

    assert codes == [200, 200, 200, 200, 200, 429], codes


# ── build_roster_rows ────────────────────────────────────────────


def test_build_roster_rows_invents_nothing_for_a_plan_sized_without_rosters() -> None:
    """A plan row with no ``student_ids`` contributes no roster row."""
    assert build_roster_rows([{"course_code": "AI313", "total_students": 40}]) == []


def test_build_roster_rows_keeps_the_plan_course_order() -> None:
    """The rows follow the plan's own order, so the file reads as the plan does."""
    Student.objects.create(student_id=9001, name="ONE", program="AI", section="M")
    Student.objects.create(student_id=9002, name="TWO", program="DS", section="M")
    plan = [
        {
            "department": "DS",
            "course_code": "DS400",
            "course_name": "DS400 NAME",
            "credit_hours": 3,
            "num_sections": 1,
            "max_per_section": 40,
            "student_ids": [9002],
        },
        {
            "department": "AI",
            "course_code": "AI400",
            "course_name": "AI400 NAME",
            "credit_hours": 4,
            "num_sections": 2,
            "max_per_section": 25,
            "student_ids": [9001, 9002],
        },
    ]

    rows = build_roster_rows(plan)

    assert [(r["course_code"], r["student_id"]) for r in rows] == [
        ("DS400", 9002),
        ("AI400", 9001),
        ("AI400", 9002),
    ]
    assert rows[0]["student_name"] == "TWO"
    assert rows[0]["programme"] == "DS"
    assert rows[2]["max_per_section"] == 25
    assert rows[2]["num_sections"] == 2


def test_build_roster_rows_names_an_unknown_student_without_inventing_one() -> None:
    """An id with no student row is still named by its id, with empty details."""
    rows = build_roster_rows([{"course_code": "AI401", "course_name": "", "student_ids": [9999]}])

    assert [(r["student_id"], r["student_name"], r["programme"]) for r in rows] == [(9999, "", "")]
