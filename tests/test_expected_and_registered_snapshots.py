"""An expected plan and the registrar's snapshot, for the same term, at once.

Every test here fails on the code as it stood before this change, and each names
the specific defect it pins:

  * the uniqueness key had no ``source``, so the two snapshots could not name the
    same section — which is every section the student registered from the plan they
    were given, i.e. exactly the overlap the comparison is about;
  * the scrape deleted ``Q(source=<scraper>) | Q(year, term)``, the second half
    matching EVERY source for the scraped term, so the first scrape of a planned
    term destroyed that term's plan;
  * ``timetable_snapshot_kind`` classified any non-plan source as registration
    evidence, so the staff planner's own mappings were shown to the student under
    "My weekly timetable" with no disclaimer.
"""

from __future__ import annotations

import re

import pytest
from django.db import IntegrityError, transaction
from django.test import Client
from django.urls import reverse

from core.models import (
    Course,
    ProgrammeRequirement,
    Student,
    StudentTermSection,
    TermSection,
    TermSectionMeeting,
)
from core.services import student_otp
from core.services.rbac import ensure_role_groups
from core.services.student_sections import (
    clear_student_section_snapshot,
    get_student_term_baseline,
    replace_student_term_sections,
)
from core.services.timetable_snapshots import (
    Snapshot,
    SnapshotClass,
    classify_source,
    effective_class,
    partition,
    select,
    timetable_snapshot_kind,
)

SID = 4970001
PROG = "SHM"
YEAR = "1448"
TERM = "1"
PLAN_SOURCE = "registration_plan_1448_t1"


# ---------------------------------------------------------------------------
# The policy itself. Pure, so these need no database.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("registration_plan_1448_t1", SnapshotClass.EXPECTED),
        ("REGISTRATION_PLAN_1449_T2", SnapshotClass.EXPECTED),
        ("  registration_plan_1448_t1  ", SnapshotClass.EXPECTED),
        ("scraper_timetable", SnapshotClass.REGISTRAR),
        ("fallback_studying", SnapshotClass.REGISTRAR),
        # The three that used to be promoted to registration evidence.
        ("planner", SnapshotClass.WORKING),
        ("auto_from_studying", SnapshotClass.WORKING),
        ("manual", SnapshotClass.WORKING),
        ("mapped", SnapshotClass.WORKING),
        ("", SnapshotClass.WORKING),
        (None, SnapshotClass.WORKING),
        ("something_nobody_has_classified", SnapshotClass.WORKING),
    ],
)
def test_classify_source(source, expected):
    assert classify_source(source) is expected


def _row(source: str, code: str) -> dict:
    return {"source": source, "course_code": code}


def test_select_returns_one_class_only():
    rows = [
        _row(PLAN_SOURCE, "AI331"),
        _row("scraper_timetable", "CS323"),
        _row("planner", "CS372"),
    ]
    assert [r["course_code"] for r in select(rows, Snapshot.REGISTERED)] == ["CS323"]
    assert [r["course_code"] for r in select(rows, Snapshot.EXPECTED)] == ["AI331"]
    assert [r["course_code"] for r in select(rows, Snapshot.WORKING)] == ["CS372"]
    assert [r["course_code"] for r in select(rows, Snapshot.ANY)] == ["AI331", "CS323", "CS372"]


def test_effective_prefers_registrar_then_working_then_expected():
    plan = _row(PLAN_SOURCE, "AI331")
    working = _row("planner", "CS372")
    registrar = _row("scraper_timetable", "CS323")

    assert effective_class([plan]) is SnapshotClass.EXPECTED
    assert effective_class([plan, working]) is SnapshotClass.WORKING
    assert effective_class([plan, working, registrar]) is SnapshotClass.REGISTRAR
    assert effective_class([]) is None

    # EFFECTIVE never mixes: with all three present only the registrar row survives.
    assert [r["course_code"] for r in select([plan, working, registrar], Snapshot.EFFECTIVE)] == [
        "CS323"
    ]


def test_select_preserves_order_within_a_class():
    rows = [_row("scraper_timetable", code) for code in ("C", "A", "B")]
    assert [r["course_code"] for r in select(rows, Snapshot.REGISTERED)] == ["C", "A", "B"]


def test_partition_always_carries_every_class():
    result = partition([_row(PLAN_SOURCE, "AI331")])
    assert set(result) == {
        SnapshotClass.EXPECTED,
        SnapshotClass.REGISTRAR,
        SnapshotClass.WORKING,
    }
    assert result[SnapshotClass.REGISTRAR] == []
    assert result[SnapshotClass.WORKING] == []


# ---------------------------------------------------------------------------
# The database rules.
# ---------------------------------------------------------------------------


@pytest.fixture
def student_with_sections(db):
    ensure_role_groups()
    Student.objects.update_or_create(
        student_id=SID,
        defaults={"name": "Both", "program": PROG, "section": "M", "gpa": 4.0},
    )
    made = {}
    for code in ("SA101", "SB201", "SC201"):
        Course.objects.update_or_create(
            course_code=code, defaults={"description": code, "credit_hours": 3}
        )
        ProgrammeRequirement.objects.update_or_create(
            program=PROG,
            course_code=code,
            defaults={"programme_term": 1, "credit_hours": 3, "type": "Mandatory"},
        )
        section = TermSection.objects.create(
            source_tag="other",
            course_name=code,
            course_code=code[:2],
            course_number=code[2:],
            course_key=code,
            section="M1",
        )
        TermSectionMeeting.objects.create(
            term_section=section,
            day="SUN",
            start_time="09:00",
            end_time="10:15",
            room="A1",
            instructor="Staff",
        )
        made[code] = section
    return made


def _link(section: TermSection, source: str, year: str = YEAR, term: str = TERM):
    return StudentTermSection.objects.create(
        student_id=SID,
        academic_year=year,
        term=term,
        term_section=section,
        source=source,
    )


def test_both_snapshots_may_name_the_same_section(student_with_sections):
    """The old key was (student, year, term, term_section) with no ``source``.

    A student who registered the very course their plan predicted could therefore
    hold only one of the two rows -- and the overlap is the majority case.
    """
    section = student_with_sections["SA101"]
    _link(section, PLAN_SOURCE)
    _link(section, "scraper_timetable")
    assert StudentTermSection.objects.filter(student_id=SID, term_section=section).count() == 2


def test_two_rows_of_the_same_source_are_still_impossible(student_with_sections):
    section = student_with_sections["SA101"]
    _link(section, "scraper_timetable")
    with pytest.raises(IntegrityError), transaction.atomic():
        _link(section, "scraper_timetable")


def test_a_scrape_does_not_delete_the_expected_plan(student_with_sections):
    """The defect: ``Q(source=scraper) | Q(year, term)`` swept the whole term."""
    _link(student_with_sections["SA101"], PLAN_SOURCE)
    _link(student_with_sections["SB201"], PLAN_SOURCE)

    replace_student_term_sections(
        SID,
        YEAR,
        TERM,
        [student_with_sections["SA101"].id],
        source="scraper_timetable",
        replace_source_across_terms="scraper_timetable",
    )

    surviving = set(
        StudentTermSection.objects.filter(student_id=SID).values_list("source", flat=True)
    )
    assert surviving == {PLAN_SOURCE, "scraper_timetable"}
    assert StudentTermSection.objects.filter(student_id=SID, source=PLAN_SOURCE).count() == 2, (
        "both planned sections must survive the scrape"
    )


def test_a_scrape_still_clears_an_older_scrape_across_terms(student_with_sections):
    _link(student_with_sections["SC201"], "scraper_timetable", year="1447", term="2")

    replace_student_term_sections(
        SID,
        YEAR,
        TERM,
        [student_with_sections["SA101"].id],
        source="scraper_timetable",
        replace_source_across_terms="scraper_timetable",
    )

    rows = list(
        StudentTermSection.objects.filter(student_id=SID).values_list("academic_year", "term")
    )
    assert rows == [(YEAR, TERM)]


def test_a_planner_write_does_not_delete_the_expected_plan(student_with_sections):
    _link(student_with_sections["SA101"], PLAN_SOURCE)

    replace_student_term_sections(
        SID, YEAR, TERM, [student_with_sections["SB201"].id], source="planner"
    )

    assert set(
        StudentTermSection.objects.filter(student_id=SID).values_list("source", flat=True)
    ) == {PLAN_SOURCE, "planner"}


def test_replace_refuses_a_source_that_disagrees_with_the_sweep(student_with_sections):
    with pytest.raises(ValueError, match="must equal source"):
        replace_student_term_sections(
            SID,
            YEAR,
            TERM,
            [student_with_sections["SA101"].id],
            source="planner",
            replace_source_across_terms="scraper_timetable",
        )


def test_clearing_the_registrar_snapshot_keeps_the_plan(student_with_sections):
    """ "The plan said three courses and the registrar recorded none" must remain
    expressible; it used to be deleted along with the empty registration."""
    _link(student_with_sections["SA101"], PLAN_SOURCE)
    _link(student_with_sections["SB201"], "scraper_timetable")

    result = clear_student_section_snapshot(SID, academic_year=YEAR, term=TERM)

    assert result["deleted"] == 1
    assert list(
        StudentTermSection.objects.filter(student_id=SID).values_list("source", flat=True)
    ) == [PLAN_SOURCE]


def test_baseline_requires_an_explicit_snapshot(student_with_sections):
    _link(student_with_sections["SA101"], PLAN_SOURCE)
    with pytest.raises(TypeError):
        get_student_term_baseline(SID, YEAR, TERM)  # type: ignore[call-arg]


def test_baseline_filters_by_snapshot(student_with_sections):
    _link(student_with_sections["SA101"], PLAN_SOURCE)
    _link(student_with_sections["SB201"], "scraper_timetable")
    _link(student_with_sections["SC201"], "planner")

    def codes(snapshot):
        return {
            row["course_code"]
            for row in get_student_term_baseline(SID, YEAR, TERM, snapshot=snapshot)
        }

    assert codes(Snapshot.EXPECTED) == {"SA101"}
    assert codes(Snapshot.REGISTERED) == {"SB201"}
    assert codes(Snapshot.WORKING) == {"SC201"}
    assert codes(Snapshot.ANY) == {"SA101", "SB201", "SC201"}
    # Registrar evidence supersedes both a forecast and a staff mapping.
    assert codes(Snapshot.EFFECTIVE) == {"SB201"}


# ---------------------------------------------------------------------------
# The screen. Asserted through the HTML, because the defect was a heading.
# ---------------------------------------------------------------------------


def _home(monkeypatch, language="en"):
    monkeypatch.setattr(
        "core.student_auth_views.load_defaults",
        lambda: {
            "academic_year": int(YEAR),
            "term": int(TERM),
            "currentYear": int(YEAR),
            "currentTerm": int(TERM),
        },
    )
    client = Client()
    client.force_login(student_otp.provision_student_user(SID))
    response = client.get(
        reverse("student_home"), headers={"accept-language": language}, SERVER_NAME="testserver"
    )
    assert response.status_code == 200, response.status_code
    return response


def test_both_timetables_are_shown_when_both_exist(student_with_sections, monkeypatch):
    _link(student_with_sections["SA101"], PLAN_SOURCE)
    _link(student_with_sections["SB201"], PLAN_SOURCE)
    _link(student_with_sections["SB201"], "scraper_timetable")

    response = _home(monkeypatch)
    body = response.content.decode()

    assert [p["kind"] for p in response.context["timetable_panels"]] == [
        "registered",
        "expected",
    ], "the registrar snapshot is what is true, so it is shown first"
    assert "My registered timetable" in body
    assert "My expected timetable" in body
    # Two independent grids, each with its own payload -- not one merged list.
    assert body.count('class="student-timetable-host"') == 2
    # The BINDING, not just the two names. `page-student-home.js` selects
    # `.student-timetable-host[data-meetings-id]` and looks the payload up by that
    # id; drop or rename the attribute and every visual grid silently goes blank
    # while the suite stays green, because nothing else in the page refers to it.
    bindings = re.findall(r'data-meetings-id="([^"]+)"', body)
    assert sorted(bindings) == [
        "studentHomeTimetableData-expected",
        "studentHomeTimetableData-registered",
    ]
    for payload_id in bindings:
        assert f'id="{payload_id}"' in body, f"no JSON payload for host binding {payload_id}"


def test_the_difference_between_the_two_is_stated(student_with_sections, monkeypatch):
    _link(student_with_sections["SA101"], PLAN_SOURCE)
    _link(student_with_sections["SB201"], PLAN_SOURCE)
    _link(student_with_sections["SB201"], "scraper_timetable")
    _link(student_with_sections["SC201"], "scraper_timetable")

    response = _home(monkeypatch)

    assert response.context["expected_not_registered"] == ["SA101"]
    assert response.context["registered_not_expected"] == ["SC201"]
    assert "Expected versus registered" in response.content.decode()


def test_no_difference_card_without_both_snapshots(student_with_sections, monkeypatch):
    _link(student_with_sections["SA101"], PLAN_SOURCE)

    response = _home(monkeypatch)

    assert response.context["expected_not_registered"] == []
    assert "Expected versus registered" not in response.content.decode()
    assert [p["kind"] for p in response.context["timetable_panels"]] == ["expected"]


def test_a_staff_planner_mapping_is_shown_as_a_forecast_never_a_registration(
    student_with_sections, monkeypatch
):
    """The mislabel: sources ``planner`` and ``auto_from_studying`` are written by
    staff-only endpoints, and the screen used to title them "My weekly timetable".

    They ARE shown -- a department's assignment is information the student needs --
    but under the forecast heading and its "not an actual registration" line.
    Hiding them would be the other failure: the student would see nothing while
    their department had them seated somewhere.
    """
    _link(student_with_sections["SA101"], "planner")
    _link(student_with_sections["SB201"], "auto_from_studying")

    response = _home(monkeypatch)
    body = response.content.decode()

    assert [p["kind"] for p in response.context["timetable_panels"]] == ["expected"]
    assert response.context["has_registered_timetable"] is False
    assert "My registered timetable" not in body
    assert "not an actual registration" in body


def test_a_planner_edit_supersedes_the_imported_plan_on_the_students_own_screen(
    student_with_sections, monkeypatch
):
    """Each writer replaces only its own class, so a planner save leaves the
    imported plan underneath it. Picking EXPECTED unconditionally would show the
    student seating their department had already moved them out of -- for ever,
    because no writer removes an imported row except a re-import.
    """
    _link(student_with_sections["SA101"], PLAN_SOURCE)
    _link(student_with_sections["SB201"], "planner")

    response = _home(monkeypatch)

    panel = response.context["timetable_panels"][0]
    assert panel["kind"] == "expected"
    assert {m["course_code"] for m in panel["meetings"]} == {"SB201"}, (
        "the department's latest forecast wins; the superseded import is not shown"
    )


def test_expected_card_says_it_is_not_a_registration(student_with_sections, monkeypatch):
    _link(student_with_sections["SA101"], PLAN_SOURCE)

    body = _home(monkeypatch).content.decode()

    assert "not an actual registration" in body


def test_arabic_headings(student_with_sections, monkeypatch):
    _link(student_with_sections["SA101"], PLAN_SOURCE)
    _link(student_with_sections["SB201"], "scraper_timetable")

    body = _home(monkeypatch, language="ar").content.decode()

    assert "جدولي المسجّل" in body
    assert "جدولي المتوقع" in body


# ---------------------------------------------------------------------------
# The label. Ten downstream surfaces turn this string into words a student reads.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sources", "kind"),
    [
        ((), "empty"),
        (("scraper_timetable",), "registered"),
        ((PLAN_SOURCE,), "expected"),
        # The degrade: a staff mapping is a forecast, so it lands in the forecast
        # bucket rather than being called registration evidence.
        (("planner",), "expected"),
        (("auto_from_studying",), "expected"),
        (("manual",), "expected"),
        ((PLAN_SOURCE, "planner"), "expected"),
        (("scraper_timetable", PLAN_SOURCE), "mixed"),
        (("scraper_timetable", "planner"), "mixed"),
    ],
)
def test_timetable_snapshot_kind_splits_on_registrar_evidence(sources, kind):
    assert timetable_snapshot_kind([_row(s, "X") for s in sources]) == kind


def test_a_staff_mapping_is_never_reported_to_a_student_as_registration(
    student_with_sections, monkeypatch
):
    """The concrete failure an adversarial review found in the first version of this
    change: the FETCH was scoped correctly while the LABEL still came from the old
    two-way rule, so `planner` rows arrived at the advisor as `REGISTERED` and the
    payload's own note told the model that means registrar evidence.
    """
    from core.services.rbac import ROLE_STUDENT
    from core.services.virtual_advisor_capabilities import _exec_my_timetable

    _link(student_with_sections["SA101"], "planner")

    result = _exec_my_timetable(
        {},
        {"role": ROLE_STUDENT, "student_id": SID},
        {"academic_year": int(YEAR), "term": int(TERM)},
    )

    assert result["schedule_kind"] != "REGISTERED"
    assert result["is_expected_plan"] is True


# ---------------------------------------------------------------------------
# The two pipelines where a merged snapshot would corrupt data rather than a
# heading: exam rooming, and group free-slot search.
# ---------------------------------------------------------------------------


def test_exam_section_enrolment_counts_registrar_rows_only(student_with_sections):
    """The plan and the registration name DIFFERENT sections of one course.

    Unfiltered, the student is counted in both -- and that count is the unit the
    exam pipeline sizes a room from, staffs invigilators from, and prints on the
    student's own exam sheet. There is no mixed-baseline guard anywhere in that
    module to catch it.
    """
    from core.services.exam_timetable import build_section_enrollment

    registered = student_with_sections["SA101"]
    planned = TermSection.objects.create(
        source_tag="other",
        course_name="SA101",
        course_code="SA",
        course_number="101",
        course_key="SA101",
        section="M2",
    )
    _link(registered, "scraper_timetable")
    _link(planned, PLAN_SOURCE)

    result = build_section_enrollment({"SA101"})

    assert [(row["section"], row["student_count"]) for row in result["SA101"]] == [("M1", 1)]


def test_group_availability_resolves_each_student_separately(student_with_sections):
    """One student registered, one only planned -- a legitimate mid-registration
    group. Resolving per query rather than per student would either drop B's week
    or book A into a slot they never registered for."""
    from core.services.group_availability import compute_group_availability

    other_id = SID + 1
    Student.objects.update_or_create(
        student_id=other_id,
        defaults={"name": "Planned only", "program": PROG, "section": "M"},
    )
    _link(student_with_sections["SA101"], "scraper_timetable")
    StudentTermSection.objects.create(
        student_id=other_id,
        academic_year=YEAR,
        term=TERM,
        term_section=student_with_sections["SB201"],
        source=PLAN_SOURCE,
    )

    result = compute_group_availability([SID, other_id], YEAR, TERM)

    assert result["no_schedule"] == [], "both students have a week on file"
    assert result["resolved_count"] == 2


def test_group_availability_ignores_a_superseded_plan(student_with_sections):
    """Both snapshots for ONE student: the registration supersedes the forecast, so
    the planned-only slot must not be counted as time that student is busy."""
    from core.services.group_availability import compute_group_availability

    _link(student_with_sections["SA101"], "scraper_timetable")
    planned_elsewhere = student_with_sections["SC201"]
    TermSectionMeeting.objects.filter(term_section=planned_elsewhere).update(
        day="TUE", start_time="13:00", end_time="14:15"
    )
    _link(planned_elsewhere, PLAN_SOURCE)

    result = compute_group_availability([SID], YEAR, TERM)

    cells_by_day = result["grids"]["lecture"]["cells"]
    busy = {
        (day, index)
        for day, cells in cells_by_day.items()
        for index, cell in enumerate(cells)
        if cell["busy_count"]
    }
    occupied_courses = {
        occupant["course_code"]
        for cells in cells_by_day.values()
        for cell in cells
        for occupant in cell["occupants"]
    }
    assert busy, "the registered meeting must still occupy its slot"
    assert "SC" not in occupied_courses, (
        "the superseded plan must not occupy time the student did not register"
    )


# ---------------------------------------------------------------------------
# The decisions themselves. A `snapshot=` argument that no test names is a
# decision anyone can reverse in a diff that stays green.
# ---------------------------------------------------------------------------


@pytest.fixture
def recorded_snapshots(monkeypatch):
    """Record every snapshot the code under test asks for."""
    from core.services import student_sections

    seen: list[str] = []
    real = student_sections.get_student_term_baseline

    def spy(*args, **kwargs):
        seen.append(str(kwargs.get("snapshot")))
        return real(*args, **kwargs)

    monkeypatch.setattr(student_sections, "get_student_term_baseline", spy)
    return seen


def test_home_cards_ask_for_registrar_evidence(student_with_sections, recorded_snapshots):
    """These cards assert credit hours and prerequisite satisfaction. A forecast
    reaching them makes the system state a registration that does not exist."""
    from core.services.student_home_cards import build_student_home_cards

    _link(student_with_sections["SA101"], PLAN_SOURCE)
    build_student_home_cards(student_id=SID, academic_year=int(YEAR), term=int(TERM))

    assert recorded_snapshots == [str(Snapshot.REGISTERED)]


def test_registered_credit_summary_asks_for_registrar_evidence(
    student_with_sections, recorded_snapshots
):
    from core.services.student_sections import get_student_term_registration_summary

    _link(student_with_sections["SA101"], PLAN_SOURCE)
    result = get_student_term_registration_summary(SID, YEAR, TERM)

    assert recorded_snapshots == [str(Snapshot.REGISTERED)]
    # An expected-only term has no registered hours, and "no evidence" is the
    # honest answer -- not zero, which is only true in one of the four cases the
    # function's docstring lists.
    assert result["known"] is False
    assert result["value"] is None


def test_the_recommendation_executor_asks_for_every_row(student_with_sections, recorded_snapshots):
    """It is the deliberate side-by-side surface, so it takes ANY and splits by
    class itself -- which is exactly the split that must not regress to a prefix
    test, because that is how a staff mapping became a registration."""
    from core.services.rbac import ROLE_STUDENT
    from core.services.virtual_advisor_capabilities import _exec_recommend_courses

    _link(student_with_sections["SA101"], "planner")

    result = _exec_recommend_courses(
        {},
        {"role": ROLE_STUDENT, "student_id": SID},
        {"academic_year": int(YEAR), "term": int(TERM)},
    )

    assert str(Snapshot.ANY) in recorded_snapshots
    assert result["ok"] is True
    codes_called_current = {row["course_code"] for row in result["already_in_current_timetable"]}
    assert "SA101" not in codes_called_current, (
        "a staff planner mapping is not a registration and must not be reported as one"
    )
    # The number the model is told the student is carrying. A staff mapping
    # contributing to it is the same defect one layer down.
    assert result["current_registered_credit_hours"] == 0


def test_a_snapshot_must_be_the_enum_not_its_spelling(student_with_sections):
    """`Snapshot` is a StrEnum, so `"registered"` would silently work while
    `"any"` raised a bare KeyError from inside the snapshot module."""
    with pytest.raises(TypeError):
        get_student_term_baseline(SID, YEAR, TERM, snapshot="registered")
    with pytest.raises(TypeError):
        get_student_term_baseline(SID, YEAR, TERM, snapshot="any")


# ---------------------------------------------------------------------------
# The shape production actually has: a term holding ONLY an imported plan.
# ---------------------------------------------------------------------------


def test_exam_enrolment_still_works_on_a_plan_only_database(student_with_sections):
    """The live database is exactly this shape -- 1525 rows, every one an imported
    plan, not one registrar row. Scoping the exam queries to registrar evidence
    emptied the whole pipeline: `build_section_enrollment` returned {} and the
    caller fell back to one synthetic "ALL" bucket carrying a single gender,
    mixing M and F students into one sitting on a gender-segregated campus.
    """
    from core.services.exam_timetable import build_section_enrollment

    _link(student_with_sections["SA101"], PLAN_SOURCE)

    result = build_section_enrollment({"SA101"})

    assert [(row["section"], row["student_count"]) for row in result["SA101"]] == [("M1", 1)]


def test_exam_enrolment_sets_resolve_per_student(student_with_sections):
    """`build_enrolled_sets_with_meta` is the live path, and it must not count a
    student twice when the plan and the registration name different sections."""
    from core.services.exam_timetable import build_enrolled_sets_with_meta

    planned = TermSection.objects.create(
        source_tag="other",
        course_name="SA101",
        course_code="SA",
        course_number="101",
        course_key="SA101",
        section="M2",
    )
    _link(student_with_sections["SA101"], "scraper_timetable")
    _link(planned, PLAN_SOURCE)

    enrolled, _meta = build_enrolled_sets_with_meta()

    assert enrolled["SA101"] == {SID}


def test_the_published_timetable_fallback_never_becomes_the_configured_terms_hours(
    student_with_sections, monkeypatch
):
    """The fallback draws ANOTHER term's week when the configured one is empty.

    Those rows belong to the panel and nowhere else. Letting them reach the
    academic cards makes «الساعات المسجّلة» — a card labelled with the configured
    term — report hours from a term the student has already finished, and drops
    the courses they took then out of their own recommendations.
    """
    from core.services.student_home_cards import build_student_home_cards

    # Registrar rows exist ONLY in an earlier term, which is the single published
    # (year, term) in the database, so the fallback fires.
    _link(student_with_sections["SA101"], "scraper_timetable", year="1447", term="2")

    response = _home(monkeypatch)

    assert response.context["timetable_is_fallback"] is True
    assert [p["kind"] for p in response.context["timetable_panels"]] == ["registered"]
    assert response.context["timetable_panels"][0]["academic_year"] == "1447"

    cards = response.context["home_cards"]
    assert cards["registered_hours"]["known"] is False, (
        "the configured term holds no registration, and 1447/2's hours are not its hours"
    )
    assert cards["registered_hours"]["academic_year"] == YEAR

    # And the cards service itself, asked directly about the configured term.
    direct = build_student_home_cards(student_id=SID, academic_year=int(YEAR), term=int(TERM))
    assert direct["registered_hours"]["known"] is False


def test_the_two_timetables_have_distinct_accessible_names(student_with_sections, monkeypatch):
    """Two grids on one page announced identically -- same region label, same
    table caption, same disclosure -- leaves a screen-reader user unable to tell
    the registrar's record from the forecast. Colour cannot carry that difference.
    """
    _link(student_with_sections["SA101"], PLAN_SOURCE)
    _link(student_with_sections["SB201"], "scraper_timetable")

    body = _home(monkeypatch).content.decode()

    for name in (
        'aria-label="My registered timetable by day and time"',
        'aria-label="My expected timetable by day and time"',
        "<summary>My registered timetable in detail</summary>",
        "<summary>My expected timetable in detail</summary>",
        'aria-label="My registered timetable details"',
        'aria-label="My expected timetable details"',
        "Your registered courses with day, start time, and end time",
        "Your expected courses with day, start time, and end time",
    ):
        assert name in body, name

    # And the JS-built agenda region, which the server cannot name for it.
    assert re.search(r'data-agenda-label="My registered timetable by day"', body)
    assert re.search(r'data-agenda-label="My expected timetable by day"', body)
