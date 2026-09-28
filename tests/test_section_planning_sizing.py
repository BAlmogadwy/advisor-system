"""Section Planning sizes courses the way the timetable builder will.

- The department plans ONE section at a time: the male section (M) or the
  female section (F), never both together. Generate and Export require it
  (400 ``section_required`` / ``section_invalid``); a plan is that section's
  alone, a student of the other section never changes it, and students with no
  recorded section are only counted, never pooled. No row, summary or export
  column carries M, F or a Total of the two.
- The page's plan for a section is the builder's sizing of that section, per
  programme and pooled.
- Declared limits hold in every view: the all-programmes view sizes a course
  by the lowest limit any programme in scope declares, through the same
  function the builder uses for a pooled scenario. A limit is the programme's,
  so both sections are sized by it.
- COE is one of our departments: its courses take the local 25/40 rules, and
  the page reads that list from the server.
- A resolved elective is planned for its slot, with the slot's declared limit,
  and slot demand that becomes no course is reported.
"""

from __future__ import annotations

import io
import json
import math
import re
from collections import Counter
from pathlib import Path

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
    ROLE_GENERAL_ADVISOR,
    ensure_role_groups,
    ensure_scope_schema,
    set_user_scope,
)
from core.services.reporting import clear_aggregate_cache
from core.services.section_planning import (
    LOCAL_DEPARTMENTS,
    compute_section_plan,
    lowest_declared_capacities,
)

pytestmark = pytest.mark.django_db

LABELS = {"M": "Male (M)", "F": "Female (F)"}


@pytest.fixture(autouse=True)
def _fresh_state():
    _rate_buckets.clear()
    clear_aggregate_cache()
    yield
    _rate_buckets.clear()
    clear_aggregate_cache()


@pytest.fixture
def planner(client: Client) -> Client:
    ensure_role_groups()
    ensure_scope_schema()
    user, _ = User.objects.get_or_create(username="sp-sizing")
    user.groups.clear()
    user.groups.add(Group.objects.get(name=ROLE_GENERAL_ADVISOR))
    set_user_scope(user.id, advisor_id="", departments="")
    client.force_login(user)
    return client


def _requirement(
    program: str, code: str, cap: int | None, *, credits: int = 3, name: str = ""
) -> None:
    ProgrammeRequirement.objects.create(
        program=program,
        course_code=code,
        course_name=name or f"{code} NAME",
        credit_hours=credits,
        type="Mandatory",
        programme_term=5,
        max_capacity=cap,
    )


def _students(program: str, first_id: int, count: int, *, section: str = "M") -> list[int]:
    ids = list(range(first_id, first_id + count))
    Student.objects.bulk_create(
        [
            Student(student_id=sid, registration_no=str(sid), program=program, section=section)
            for sid in ids
        ]
    )
    return ids


def _recommend(
    monkeypatch: pytest.MonkeyPatch, by_program: dict[str, list[str]], *, builder: bool = False
) -> None:
    """Every student of a programme is recommended that programme's courses
    (on the page; and in the timetable builder when ``builder``)."""

    def single(student_ids, program, _year, _term, **_kw):
        return {sid: list(by_program.get(program, [])) for sid in student_ids}

    def multi(student_ids, _year, _term, **_kw):
        programs = dict(
            Student.objects.filter(student_id__in=student_ids).values_list("student_id", "program")
        )
        return {sid: list(by_program.get(programs[sid], [])) for sid in student_ids}

    modules = ["core.services.reporting"]
    if builder:
        modules.append("core.services.recommender_batch")
    for module in modules:
        monkeypatch.setattr(f"{module}.batch_recommend", single)
        monkeypatch.setattr(f"{module}.batch_recommend_multi_program", multi)


def _recommend_each(monkeypatch: pytest.MonkeyPatch, by_student: dict[int, list[str]]) -> None:
    """Each student is recommended their own courses (read at call time)."""

    def single(student_ids, _program, _year, _term, **_kw):
        return {sid: list(by_student.get(sid, [])) for sid in student_ids}

    def multi(student_ids, _year, _term, **_kw):
        return {sid: list(by_student.get(sid, [])) for sid in student_ids}

    monkeypatch.setattr("core.services.reporting.batch_recommend", single)
    monkeypatch.setattr("core.services.reporting.batch_recommend_multi_program", multi)


def _post(client: Client, endpoint: str, body: dict):
    _rate_buckets.clear()  # these tests generate more often than a person may
    return client.post(
        f"/ops/section-planning/{endpoint}/",
        json.dumps({"year": 1448, "semester": 1, **body}),
        content_type="application/json",
    )


def _generate(client: Client, section: str, **body) -> dict:
    response = _post(client, "generate", {"section": section, **body})
    assert response.status_code == 200, response.content
    return response.json()


def _row(plan: list[dict], code: str) -> dict:
    rows = [row for row in plan if row["course_code"] == code]
    assert len(rows) == 1, rows
    return rows[0]


def _export(client: Client, section: str, **body):
    """The response, and its workbook read as a CI machine reads it (no lxml there)."""
    from openpyxl import load_workbook

    response = _post(client, "export", {"section": section, **body})
    assert response.status_code == 200, response.content
    content = b"".join(response.streaming_content)
    return response, load_workbook(io.BytesIO(content), data_only=False)


def _sheet_rows(sheet) -> list[dict]:
    headers = [cell.value for cell in sheet[2]]
    return [
        dict(zip(headers, [cell.value for cell in row], strict=False))
        for row in sheet.iter_rows(min_row=3)
        if row[0].value is not None
    ]


def _export_rows(client: Client, section: str, **body) -> list[dict]:
    return _sheet_rows(_export(client, section, **body)[1].worksheets[0])


def _keys(value) -> list[str]:
    """Every key in a JSON answer, at any depth."""
    if isinstance(value, dict):
        return [*value, *(k for v in value.values() for k in _keys(v))]
    if isinstance(value, list):
        return [k for v in value for k in _keys(v)]
    return []


# ── one section per plan ─────────────────────────────────────────


MISSING = object()


@pytest.mark.parametrize("endpoint", ["generate", "export"])
@pytest.mark.parametrize(
    ("section", "code"),
    [
        (MISSING, "section_required"),
        (None, "section_required"),
        ("", "section_required"),
        ("   ", "section_required"),
        ("X", "section_invalid"),
        ("MF", "section_invalid"),
        ("M,F", "section_invalid"),
        ("male", "section_invalid"),
        (1, "section_invalid"),
        (["M"], "section_invalid"),
    ],
)
def test_generate_and_export_require_one_section(
    planner: Client, endpoint: str, section, code: str
) -> None:
    body = {} if section is MISSING else {"section": section}

    response = _post(planner, endpoint, {"program": "AI", **body})

    assert response.status_code == 400
    assert response.json()["code"] == code


@pytest.fixture
def mixed_sections(monkeypatch: pytest.MonkeyPatch) -> None:
    """AI: 12 men and 30 women need AI331 (4 cr, local: 25 a section); 3
    students have no recorded section, and they alone also need AI352."""
    Course.objects.create(
        course_code="AI331", department="AI", credit_hours=4, description="MACHINE LEARNING"
    )
    Course.objects.create(
        course_code="AI352", department="AI", credit_hours=3, description="AI352 NAME"
    )
    _requirement("AI", "AI331", None, credits=4, name="MACHINE LEARNING")
    _requirement("AI", "AI352", None)
    men = _students("AI", 461000001, 12, section="M")
    women = _students("AI", 462000001, 30, section=" f ")  # recorded, only badly spelt
    unknown = _students("AI", 463000001, 3, section="")
    by_student = {sid: ["AI331"] for sid in men + women}
    by_student.update({sid: ["AI331", "AI352"] for sid in unknown})
    _recommend_each(monkeypatch, by_student)


@pytest.mark.parametrize("program", ["AI", None])
@pytest.mark.parametrize(("section", "students", "sections"), [("M", 12, 1), ("F", 30, 2)])
def test_a_plan_is_the_chosen_sections_alone(
    planner: Client, mixed_sections, program, section, students, sections
) -> None:
    data = _generate(planner, section, **({"program": program} if program else {}))

    # AI352 only students with no recorded section need: in neither plan.
    (row,) = data["plan"]
    assert row["course_code"] == "AI331"
    assert (row["total_students"], row["num_sections"], row["max_per_section"]) == (
        students,
        sections,
        25,
    )
    assert data["section"] == section
    assert (data["student_count"], data["no_section"]) == (students, 3)
    summary = data["summary"]
    assert (summary["total_courses"], summary["total_sections"], summary["total_students"]) == (
        1,
        sections,
        students,
    )
    (ai,) = summary["departments"]
    assert (ai["courses"], ai["sections"], ai["students"]) == (1, sections, students)


@pytest.mark.parametrize("section", ["m", " F ", "f"])
def test_the_section_is_read_trimmed_in_either_case(
    planner: Client, mixed_sections, section
) -> None:
    data = _generate(planner, section, program="AI")

    assert data["section"] == section.strip().upper()


@pytest.mark.parametrize("program", ["AI", None, "AI,DS"])
def test_no_answer_carries_male_female_or_their_total(
    planner: Client, mixed_sections, program
) -> None:
    data = _generate(planner, "F", **({"program": program} if program else {}))

    split = [k for k in _keys(data) if re.search(r"male|gender|cohort|unknown", k)]
    assert split == []
    rows = data.get("plan") or data["combined_plan"]
    assert {row["status"] for row in rows} <= {"", "full", "underfilled"}


@pytest.fixture
def elective_spread(monkeypatch: pytest.MonkeyPatch) -> dict[int, list[str]]:
    """Slot AI1 (30 seats) resolves to AI463 or AI464; the resolver spreads a
    slot's students across the courses it resolves to. Three women need it."""
    Course.objects.create(course_code="AI331", department="AI", credit_hours=4)
    Course.objects.create(course_code="AI463", department="AI", credit_hours=3)
    Course.objects.create(course_code="AI464", department="AI", credit_hours=3)
    _requirement("AI", "AI331", None, credits=4)
    _slot("AI", "AI1", 30)
    _publish("AI", "AI1", "AI463")
    _publish("AI", "AI1", "AI464")
    wants = {sid: ["AI1"] for sid in _students("AI", 512000001, 3, section="F")}
    _recommend_each(monkeypatch, wants)
    return wants


@pytest.mark.parametrize("program", ["AI", None])
def test_a_student_of_the_other_section_never_changes_the_plan(
    planner: Client, elective_spread, program
) -> None:
    """Not even which course a slot's students are spread across: that is
    decided among the section's own students, as the builder decides it."""
    scope = {"program": program} if program else {}
    before = _generate(planner, "F", **scope)
    assert {r["course_code"]: r["total_students"] for r in before["plan"]} == {
        "AI463": 2,
        "AI464": 1,
    }

    # 25 men, listed before the women, need AI1 and AI331 too.
    for sid in _students("AI", 511000001, 25, section="M"):
        elective_spread[sid] = ["AI1", "AI331"]
    clear_aggregate_cache()
    after = _generate(planner, "F", **scope)

    assert after == before
    men = _generate(planner, "M", **scope)
    assert {r["course_code"]: r["total_students"] for r in men["plan"]} == {
        "AI331": 25,
        "AI463": 13,
        "AI464": 12,
    }


# ── the page plans a section as the builder sizes it ──────────────


@pytest.fixture
def two_programmes(monkeypatch: pytest.MonkeyPatch) -> None:
    """AI and DS share CS211 (AI declares 30 a section, DS 35) and MATH203
    (external: 50); AI331 is AI's own, DS201 DS's. The sections differ in size."""
    Course.objects.create(
        course_code="CS211", department="CS", credit_hours=3, description="DATA STRUCTURES"
    )
    Course.objects.create(
        course_code="MATH203",
        department="MATH",
        credit_hours=3,
        description="CALCULUS I",
        is_external=True,
    )
    Course.objects.create(
        course_code="AI331", department="AI", credit_hours=4, description="MACHINE LEARNING"
    )
    Course.objects.create(
        course_code="DS201", department="DS", credit_hours=3, description="DATA SCIENCE"
    )
    for program, cap in (("AI", 30), ("DS", 35)):
        _requirement(program, "CS211", cap, name="DATA STRUCTURES")
        _requirement(program, "MATH203", None, name="CALCULUS I")
    _requirement("AI", "AI331", None, credits=4, name="MACHINE LEARNING")
    _requirement("DS", "DS201", None, name="DATA SCIENCE")
    _students("AI", 521000001, 34, section="M")
    _students("AI", 522000001, 57, section="F")
    _students("DS", 523000001, 23, section="M")
    _students("DS", 524000001, 41, section="F")
    _students("AI", 525000001, 4, section="")  # in neither plan
    _recommend(
        monkeypatch,
        {"AI": ["CS211", "MATH203", "AI331"], "DS": ["CS211", "MATH203", "DS201"]},
        builder=True,
    )


def _sized(rows: list[dict]) -> dict[str, tuple[int, int, int]]:
    return {
        row["course_key"]: (row["total_students"], row["max_per_section"], row["num_sections"])
        for row in rows
    }


@pytest.mark.parametrize(("section", "cs211_sections"), [("M", 2), ("F", 4)])
def test_the_page_plans_a_section_as_the_builder_sizes_it(
    planner: Client, two_programmes, section, cs211_sections
) -> None:
    """Per programme and pooled, every course has the sections and the limit
    the builder creates for that section."""
    from core.services.timetable_generate import generate_workspace_scenario

    def built(program) -> dict:
        name = f"parity {section} {program}"
        return _sized(
            generate_workspace_scenario(
                1448, 1, program, section=section, scenario_name=name, run_autoplace=False
            )["section_plan"]
        )

    page = _generate(planner, section, program="AI,DS")

    pooled = _sized(page["combined_plan"])
    assert pooled == built(["AI", "DS"])
    # Pooled, CS211 takes the lowest declared limit (AI's 30) for 57 men or 98 women.
    assert pooled["CS211::DATA_STRUCTURES"][1:] == (30, cs211_sections)
    assert [entry["program"] for entry in page["programs"]] == ["AI", "DS"]
    for entry in page["programs"]:
        program = entry["program"]
        builder = built(program)
        assert _sized(entry["plan"]) == builder, program
        assert _sized(_generate(planner, section, program=program)["plan"]) == builder, program


# ── declared limits in every view ─────────────────────────────────


def test_lowest_declared_capacities_takes_the_most_restrictive_programme() -> None:
    _requirement("AI", "CS211", 30)
    _requirement("DS", "CS211", 25)
    _requirement("CS", "CS211", 20)  # not in scope: must not count
    _requirement("AI", "AI491", 5)
    _requirement("DS", "AI491", None)
    _requirement("AI", "MATH203", 0)  # a zero is not a declaration
    metadata = {
        "CS211::DATA_STRUCTURES": {"course_code": "CS211"},
        "AI491": {"course_code": "AI491"},
        "MATH203": {"course_code": "MATH203"},
        "PHYS103": {},
    }

    caps = lowest_declared_capacities(["AI", "DS"], metadata)

    assert caps == {"CS211::DATA_STRUCTURES": 25, "AI491": 5}
    assert lowest_declared_capacities([], metadata) == {}


@pytest.fixture
def graduation_project(monkeypatch: pytest.MonkeyPatch) -> None:
    """31 AI students need AI491, which AI limits to 5 per section; DS limits nothing."""
    Course.objects.create(
        course_code="AI491", department="AI", credit_hours=2, description="GRADUATION PROJECT I"
    )
    Course.objects.create(
        course_code="DS321", department="DS", credit_hours=3, description="DS321 NAME"
    )
    _requirement("AI", "AI491", 5, credits=2, name="GRADUATION PROJECT I")
    _requirement("AI2", "AI491", None, credits=2, name="GRADUATION PROJECT I")
    _requirement("DS", "DS321", None)
    _students("AI", 441000001, 16, section="M")
    _students("AI", 442000001, 15, section="F")
    _students("DS", 443000001, 4, section="M")
    _recommend(monkeypatch, {"AI": ["AI491"], "DS": ["DS321"]})


@pytest.mark.parametrize(("section", "sections"), [("M", 4), ("F", 3)])
def test_ai491_has_the_same_max_in_its_programme_and_in_all_programmes(
    planner: Client, graduation_project, section, sections
) -> None:
    programme_view = _row(_generate(planner, section, program="AI")["plan"], "AI491")
    default_view = _row(_generate(planner, section)["plan"], "AI491")

    assert programme_view["max_per_section"] == 5
    assert default_view["max_per_section"] == programme_view["max_per_section"]
    assert default_view["num_sections"] == programme_view["num_sections"] == sections


def test_the_all_programmes_export_carries_the_declared_limit(
    planner: Client, graduation_project
) -> None:
    row = next(r for r in _export_rows(planner, "F") if r["Course"] == "AI491")
    assert row["Max/Section"] == 5


# ── COE is one of our departments ────────────────────────────────


def test_coe_courses_take_the_local_rules() -> None:
    Course.objects.create(course_code="COE211", department="COE", credit_hours=4)
    Course.objects.create(course_code="COE321", department="", credit_hours=3)
    Course.objects.create(course_code="MATH203", department="MATH", credit_hours=4)

    plan = {
        row["course_code"]: row
        for row in compute_section_plan(Counter({"COE211": 60, "COE321": 60, "MATH203": 60}))
    }

    assert "COE" in LOCAL_DEPARTMENTS
    assert plan["COE211"]["max_per_section"] == 25  # local, 4 credits
    assert plan["COE211"]["num_sections"] == 3
    assert (
        plan["COE321"]["max_per_section"] == 40
    )  # local, other credits (department from the code)
    assert plan["COE321"]["is_external"] is False
    assert plan["MATH203"]["max_per_section"] == 50  # a service department stays external


def test_a_coe_course_flagged_external_keeps_the_external_rule() -> None:
    """COE joining our departments does not reinterpret the per-course flag."""
    Course.objects.create(course_code="COE466", department="COE", credit_hours=3, is_external=True)

    (row,) = compute_section_plan(Counter({"COE466": 60}))

    assert row["max_per_section"] == 50


def test_the_page_is_given_the_servers_department_list(planner: Client) -> None:
    html = planner.get("/section-planning/").content.decode("utf-8")
    embedded = re.search(
        r'<script id="spLocalDepartments" type="application/json">(.*?)</script>', html
    )

    assert embedded, "the page must be handed the server's list"
    assert json.loads(embedded.group(1)) == sorted(LOCAL_DEPARTMENTS)
    js = (Path(__file__).resolve().parents[1] / "static/js/page-section-planning.js").read_text(
        encoding="utf-8"
    )
    assert "'CYB'" not in js and '"CYB"' not in js, "the page must not keep a list of its own"


# ── programmes pooled within a section ────────────────────────────


def test_several_programmes_are_pooled_within_the_section(
    planner: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AI and DS share CS211 (limits 30 and 35): a section pools its students across programmes."""
    Course.objects.create(
        course_code="CS211", department="CS", credit_hours=3, description="DATA STRUCTURES"
    )
    _requirement("AI", "CS211", 30, name="DATA STRUCTURES")
    _requirement("DS", "CS211", 35, name="DATA STRUCTURES")
    _students("AI", 471000001, 20, section="M")
    _students("DS", 472000001, 20, section="M")
    _students("AI", 473000001, 5, section="F")
    _recommend(monkeypatch, {"AI": ["CS211"], "DS": ["CS211"]})

    men = _generate(planner, "M", program="AI,DS")
    women = _generate(planner, "F", program="AI,DS")

    row = _row(men["combined_plan"], "CS211")
    assert row["programs"] == ["AI", "DS"]
    assert (row["total_students"], row["max_per_section"], row["num_sections"]) == (40, 30, 2)
    assert men["combined_summary"]["total_sections"] == 2
    assert men["student_count"] == 40
    per_programme = {p["program"]: _row(p["plan"], "CS211") for p in men["programs"]}
    assert (per_programme["AI"]["max_per_section"], per_programme["AI"]["num_sections"]) == (30, 1)
    assert (per_programme["DS"]["max_per_section"], per_programme["DS"]["num_sections"]) == (35, 1)
    # The women's plan: only AI has women; DS's plan for them is empty.
    row = _row(women["combined_plan"], "CS211")
    assert (row["programs"], row["total_students"], row["num_sections"]) == (["AI"], 5, 1)
    assert {p["program"]: len(p["plan"]) for p in women["programs"]} == {"AI": 1, "DS": 0}
    # The Department Summary adds up to the KPI (it is the pooled plan's).
    assert sum(d["sections"] for d in men["combined_summary"]["departments"]) == 2


def test_same_code_different_plan_names_stay_two_courses(
    planner: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    Course.objects.create(
        course_code="CS111", department="CS", credit_hours=4, description="GLOBAL NAME"
    )
    _requirement("AI", "CS111", 20, credits=4, name="PROGRAMMING I")
    _requirement("AI2", "CS111", None, credits=4, name="FUNDAMENTALS OF PROGRAMMING")
    _requirement("DS", "CS111", 30, credits=4, name="PROGRAMMING I")
    _students("AI", 481000001, 10)
    _students("AI2", 482000001, 4)
    _students("DS", 483000001, 15)
    _recommend(monkeypatch, {"AI": ["CS111"], "AI2": ["CS111"], "DS": ["CS111"]})

    data = _generate(planner, "M", program="AI,AI2,DS")

    rows = {
        row["course_name"]: row for row in data["combined_plan"] if row["course_code"] == "CS111"
    }
    assert set(rows) == {"PROGRAMMING I", "FUNDAMENTALS OF PROGRAMMING"}, "the plan's own names win"
    assert rows["PROGRAMMING I"]["programs"] == ["AI", "DS"]
    assert rows["PROGRAMMING I"]["total_students"] == 25
    assert rows["PROGRAMMING I"]["max_per_section"] == 20
    assert rows["PROGRAMMING I"]["num_sections"] == 2
    assert rows["FUNDAMENTALS OF PROGRAMMING"]["programs"] == ["AI2"]
    assert rows["FUNDAMENTALS OF PROGRAMMING"]["total_students"] == 4


def test_a_programme_listed_twice_is_counted_once(planner: Client, mixed_sections) -> None:
    data = _generate(planner, "M", program="AI,AI")

    assert data["mode"] == "single"
    assert data["student_count"] == 12
    assert _row(data["plan"], "AI331")["total_students"] == 12


# ── the export is the chosen section's plan ───────────────────────

SECTIONS_HEADERS = [
    "#",
    "Department",
    "Course",
    "Name",
    "Credits",
    "External",
    "Students",
    "Max/Section",
    "Sections",
    "Avg/Section",
    "Fill %",
    "Status",
    "Limit from",
    "Fills slot",
]


@pytest.mark.parametrize(("section", "students", "sections"), [("M", 12, 1), ("F", 30, 2)])
def test_the_export_is_the_chosen_sections_plan(
    planner: Client, mixed_sections, section, students, sections
) -> None:
    response, workbook = _export(planner, section, program="AI")
    sheet, summary = workbook.worksheets

    assert f'filename="section_plan_1448_1_AI_{section}.xlsx"' in response["Content-Disposition"]
    assert [cell.value for cell in sheet[2]] == SECTIONS_HEADERS
    label = LABELS[section]
    assert sheet.cell(row=1, column=1).value == (
        f"Section Planning — Section: {label} — 3 students have no recorded section and are"
        " not in this plan."
    )
    (row,) = _sheet_rows(sheet)
    assert (row["Course"], row["Students"], row["Max/Section"]) == ("AI331", students, 25)
    # Computed as the page computes them, from Students (G) and Max/Section (H).
    assert row["Sections"] == "=IF(G3>0,CEILING(G3/H3,1),0)"
    assert row["Avg/Section"] == "=IF(I3>0,CEILING(G3/I3,1),0)"
    assert row["Fill %"] == '=IF(AND(H3>0,I3>0),ROUND(J3/H3*100,0)&"%","")'
    assert math.ceil(row["Students"] / row["Max/Section"]) == sections

    assert summary.cell(row=1, column=1).value == (
        f"Section Plan Summary — 1448/1 — Section: {label}"
    )
    kpis = {
        summary.cell(row=r, column=c).value: summary.cell(row=r, column=c + 1).value
        for r in (3, 4)
        for c in (1, 3, 5)
    }
    assert kpis == {
        "Section": label,
        "Students": students,
        "No recorded section": 3,
        "Courses": '=COUNTIF(Sections!I3:I3,">0")',
        "Sections": "=SUM(Sections!I3:I3)",
        "Seat demand": "=SUM(Sections!G3:G3)",
    }
    assert summary.cell(row=5, column=1).value == (
        f"{label} students only: the other section is planned on its own. 3 students have no"
        " recorded section and are not in this plan."
    )
    assert [summary.cell(row=8, column=c).value for c in range(1, 6)] == [
        "Department",
        "Sections",
        "Courses",
        "Seat demand",
        "Teaching hours",
    ]
    words = " ".join(
        str(cell.value)
        for ws in (sheet, summary)
        for line in ws.iter_rows()
        for cell in line
        if cell.value is not None
    )
    other = LABELS["F" if section == "M" else "M"]
    assert other not in words and "gender" not in words.lower()


@pytest.fixture
def departments(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two programmes, ours and service departments, both sections."""
    _requirement("AI", "AI331", None, credits=4)
    _requirement("AI", "MATH203", None)
    _requirement("DS", "DS201", None)
    _requirement("DS", "MATH203", None)
    _students("AI", 491000001, 30, section="M")
    _students("AI", 492000001, 45, section="F")
    _students("DS", 493000001, 10, section="F")
    _students("DS", 494000001, 7, section="M")
    _recommend(monkeypatch, {"AI": ["AI331", "MATH203"], "DS": ["DS201", "MATH203"]})


@pytest.mark.parametrize("section", ["M", "F"])
@pytest.mark.parametrize("program", ["AI", None, "AI,DS"])
def test_the_summary_lists_every_department_and_adds_up_to_its_totals(
    planner: Client, departments, program, section
) -> None:
    """The page's Department Summary shows every department, ours and the service
    departments; its total row equals the KPIs because these add up."""
    data = _generate(planner, section, **({"program": program} if program else {}))

    if data["mode"] == "multi":
        summaries = [data["combined_summary"], *(p["summary"] for p in data["programs"])]
    else:
        summaries = [data["summary"]]
    for summary in summaries:
        depts = summary["departments"]
        assert sum(d["sections"] for d in depts) == summary["total_sections"] > 0
        assert sum(d["courses"] for d in depts) == summary["total_courses"]
        assert sum(d["students"] for d in depts) == summary["total_students"]
    shown = {d["department"] for d in summaries[0]["departments"]}
    assert {"AI", "MATH"} <= shown
    assert "MATH" not in LOCAL_DEPARTMENTS, "a service department, listed too"


@pytest.mark.parametrize("section", ["M", "F"])
@pytest.mark.parametrize(
    ("program", "sheet_name"), [("AI", "Sections"), (None, "Sections"), ("AI,DS", "Sections-All")]
)
def test_the_exports_totals_are_the_pages(
    planner: Client, departments, program, sheet_name, section
) -> None:
    scope = {"program": program} if program else {}
    data = _generate(planner, section, **scope)
    summary = data.get("summary") or data["combined_summary"]
    _response, workbook = _export(planner, section, **scope)
    rows = _sheet_rows(workbook[sheet_name])

    # The Sections column is =CEILING(Students/Max): as Excel will compute it.
    sections = [math.ceil(r["Students"] / r["Max/Section"]) for r in rows]
    assert sum(sections) == summary["total_sections"]
    assert len(rows) == summary["total_courses"]
    assert sum(r["Students"] for r in rows) == summary["total_students"]
    by_dept: Counter[str] = Counter()
    for r, n in zip(rows, sections, strict=True):
        by_dept[r["Department"]] += n
    assert dict(by_dept) == {d["department"]: d["sections"] for d in summary["departments"]}
    # The Summary sheet totals exactly those cells.
    sheet = workbook[sheet_name.replace("Sections", "Summary")]
    ref = f"'{sheet_name}'" if "-" in sheet_name else sheet_name
    last = 2 + len(rows)
    assert sheet.cell(row=4, column=4).value == f"=SUM({ref}!I3:I{last})"
    assert sheet.cell(row=3, column=4).value == data["student_count"]


def test_the_multi_programme_export_writes_the_pooled_plan_first(
    planner: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    Course.objects.create(
        course_code="CS211", department="CS", credit_hours=3, description="DATA STRUCTURES"
    )
    _requirement("AI", "CS211", 30, name="DATA STRUCTURES")
    _requirement("DS", "CS211", 35, name="DATA STRUCTURES")
    _students("AI", 491000001, 20, section="M")
    _students("DS", 492000001, 20, section="M")
    _students("DS", 493000001, 5, section="F")
    _recommend(monkeypatch, {"AI": ["CS211"], "DS": ["CS211"]})

    response, workbook = _export(planner, "M", program="AI,DS")

    assert 'filename="section_plan_1448_1_multi_M.xlsx"' in response["Content-Disposition"]
    assert workbook.sheetnames[:2] == ["Sections-All", "Summary-All"]
    assert "Sections-AI" in workbook.sheetnames and "Sections-DS" in workbook.sheetnames
    (pooled,) = _sheet_rows(workbook["Sections-All"])
    assert (pooled["Students"], pooled["Max/Section"]) == (40, 30)
    assert pooled["Name"] == "AI, DS - DATA STRUCTURES"
    (ds,) = _sheet_rows(workbook["Sections-DS"])
    assert (ds["Students"], ds["Max/Section"]) == (20, 35), "DS's men only"


# ── a resolved elective takes its slot's declared limit ──────────


def _slot(program: str, code: str, cap: int | None) -> None:
    ProgrammeRequirement.objects.create(
        program=program,
        course_code=code,
        course_name=f"DEPARTMENT ELECTIVE {code[-1]}",
        credit_hours=3,
        type="Program Elective",
        programme_term=7,
        max_capacity=cap,
    )


def _publish(program: str, slot: str, code: str, *, prerequisites: str = "") -> None:
    elective = ElectiveCourse.objects.create(
        programme=program,
        course_code=code,
        course_name=f"{code} ELECTIVE",
        credit_hours=3,
        prerequisites_csv=prerequisites,
    )
    ElectiveTermMapping.objects.create(
        academic_year="1448", term=1, programme=program, placeholder_code=slot, elective=elective
    )


@pytest.fixture
def electives(monkeypatch: pytest.MonkeyPatch):
    """AI1 (30 seats) resolves to AI463, a course flagged external; AI2 is not
    published this term; AI3 resolves only to a course nobody is eligible for.
    70 men and 52 women need AI1; 5 men need AI2; 3 women need AI3."""
    Course.objects.create(course_code="AI463", department="AI", credit_hours=3, is_external=True)
    _slot("AI", "AI1", 30)
    _slot("AI", "AI2", 30)
    _slot("AI", "AI3", None)
    _publish("AI", "AI1", "AI463")
    _publish("AI", "AI3", "AI464", prerequisites="AI999")
    men = _students("AI", 501000001, 70, section="M")
    women = _students("AI", 502000001, 52, section="F")
    unpublished = _students("AI", 503000001, 5, section="M")
    blocked = _students("AI", 504000001, 3, section="F")
    wants = {
        **{sid: ["AI1"] for sid in men + women},
        **{sid: ["AI2"] for sid in unpublished},
        **{sid: ["AI3"] for sid in blocked},
    }
    _recommend_each(monkeypatch, wants)


@pytest.mark.parametrize("program", ["AI", None])
@pytest.mark.parametrize(("section", "sections"), [("M", 3), ("F", 2)])  # 70/30, 52/30
def test_a_resolved_elective_takes_its_slots_declared_limit(
    planner: Client, electives, program, section, sections
) -> None:
    data = _generate(planner, section, **({"program": program} if program else {}))
    row = _row(data["plan"], "AI463")

    assert row["is_external"] is True
    assert row["max_per_section"] == 30, "the AI1 slot's limit, not the external 50"
    assert row["num_sections"] == sections
    assert row["limit_source"] == "slot"
    assert row["slots"] == ["AI1"]


def test_a_slot_that_declares_nothing_leaves_the_course_to_its_own_rule(
    planner: Client, electives
) -> None:
    ProgrammeRequirement.objects.filter(program="AI", course_code="AI1").update(max_capacity=None)

    row = _row(_generate(planner, "M", program="AI")["plan"], "AI463")

    assert row["max_per_section"] == 50
    assert row["limit_source"] == "rule"
    assert row["slots"] == ["AI1"]


def test_a_draft_on_the_slot_is_a_what_if_for_its_elective(planner: Client, electives) -> None:
    row = _row(_generate(planner, "F", program="AI", course_overrides={"AI1": 20})["plan"], "AI463")

    assert row["max_per_section"] == 20
    assert row["limit_source"] == "draft"
    assert ProgrammeRequirement.objects.get(program="AI", course_code="AI1").max_capacity == 30


@pytest.mark.parametrize(
    ("section", "dropped"),
    [
        ("M", {"program": "AI", "slot": "AI2", "reason": "not_published", "students": 5}),
        ("F", {"program": "AI", "slot": "AI3", "reason": "no_eligible_course", "students": 3}),
    ],
)
def test_slot_demand_that_becomes_no_course_is_reported_for_its_section(
    planner: Client, electives, section, dropped
) -> None:
    data = _generate(planner, section, program="AI")

    assert data["electives"] == {"dropped": [dropped], "dropped_total": dropped["students"]}
    assert {row["course_code"] for row in data["plan"]} == {"AI463"}


def test_the_panel_lists_the_elective_under_its_slot(planner: Client, electives) -> None:
    response = planner.get(
        "/ops/section-planning/courses/", {"program": "AI", "year": 1448, "semester": 1}
    )
    courses = {c["course_code"]: c for c in response.json()["courses"]}

    assert courses["AI1"]["slot_electives"] == [
        {"program": "AI", "status": "ready", "courses": ["AI463"]}
    ]
    assert courses["AI2"]["slot_electives"] == [
        {"program": "AI", "status": "not_published", "courses": []}
    ]
    without_term = planner.get("/ops/section-planning/courses/", {"program": "AI"}).json()
    assert {c["course_code"]: c["slot_electives"] for c in without_term["courses"]}["AI1"] == []


def test_the_export_names_the_slot_and_the_limits_source(planner: Client, electives) -> None:
    _response, workbook = _export(planner, "M", program="AI")
    (row,) = [r for r in _sheet_rows(workbook.worksheets[0]) if r["Course"] == "AI463"]

    assert row["Max/Section"] == 30
    assert row["Limit from"] == "Slot AI1 limit"
    assert row["Fills slot"] == "AI1"
    note = workbook.worksheets[1].cell(row=6, column=1).value
    assert note == (
        "Elective-slot demand that became no course: 5 — AI AI2: not published for this term (5)"
    )


def test_an_elective_filling_slots_with_different_limits_takes_the_lowest(
    planner: Client, electives, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AI2 students reach AI463 through AI's publication, for AI2's own AI1 slot (25)."""
    _slot("AI2", "AI1", 25)
    ai2 = _students("AI2", 505000001, 10, section="M")
    wants = {sid: ["AI1"] for sid in Student.objects.values_list("student_id", flat=True)}
    wants.update({sid: ["AI2"] for sid in range(503000001, 503000006)})
    wants.update({sid: ["AI3"] for sid in range(504000001, 504000004)})
    monkeypatch.setattr(
        "core.services.reporting.batch_recommend",
        lambda student_ids, _program, _year, _term, **_kw: {
            sid: list(wants[sid]) for sid in student_ids
        },
    )

    data = _generate(planner, "M", program="AI,AI2")

    pooled = _row(data["combined_plan"], "AI463")
    assert pooled["max_per_section"] == 25
    assert pooled["total_students"] == 70 + len(ai2)
    per_programme = {
        p["program"]: _row(p["plan"], "AI463")["max_per_section"] for p in data["programs"]
    }
    assert per_programme == {"AI": 30, "AI2": 25}


def test_the_builder_sizes_a_resolved_elective_by_its_slot_as_section_planning_does(
    planner: Client, electives, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One slot rule: the sections the builder creates are the ones the page shows."""
    from core.services.timetable_generate import generate_workspace_scenario

    men = list(range(501000001, 501000071))
    monkeypatch.setattr("core.services.timetable_generate.get_student_ids", lambda **_kw: men)
    monkeypatch.setattr(
        "core.services.recommender_batch.batch_recommend",
        lambda student_ids, _program, _year, _term, **_kw: {sid: ["AI1"] for sid in student_ids},
    )

    built = generate_workspace_scenario(
        1448, 1, "AI", section="M", scenario_name="slot rule", run_autoplace=False
    )
    planned = _row(_generate(planner, "M", program="AI")["plan"], "AI463")

    builder_row = _row(built["section_plan"], "AI463")
    assert builder_row["max_per_section"] == 30, "the AI1 slot's limit, not the external 50"
    assert builder_row["max_per_section"] == planned["max_per_section"]
    assert builder_row["num_sections"] == planned["num_sections"] == 3

    what_if = generate_workspace_scenario(
        1448,
        1,
        "AI",
        section="M",
        scenario_name="slot what-if",
        course_overrides={"AI1": 20},
        run_autoplace=False,
    )
    assert _row(what_if["section_plan"], "AI463")["max_per_section"] == 20


def test_the_all_programmes_panel_starts_from_the_limit_generate_applies(
    planner: Client, graduation_project
) -> None:
    """No programme on screen: the panel shows the lowest declared limit, not the rule."""
    courses = {
        c["course_code"]: c for c in planner.get("/ops/section-planning/courses/").json()["courses"]
    }

    assert courses["AI491"]["default_max"] == 40
    assert courses["AI491"]["programme_max"] == 5
    assert courses["AI491"]["limit_scope"] == "lowest_declared"
    assert courses["DS321"]["programme_max"] is None
    for section in ("M", "F"):
        plan_row = _row(_generate(planner, section)["plan"], "AI491")
        assert plan_row["max_per_section"] == courses["AI491"]["programme_max"], section
