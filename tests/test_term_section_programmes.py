from __future__ import annotations

import csv
import importlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import TypedDict, cast

import pytest
from django.apps import apps
from django.contrib.auth.models import Group, User
from django.db import connection
from django.test import Client

from core.models import (
    ProgrammeRequirement,
    Student,
    StudentTermSection,
    TermSection,
    TermSectionMeeting,
    TermSectionProgram,
    TimetableScenario,
)
from core.services.rbac import ROLE_SUPER_ADMIN, ensure_role_groups
from core.services.section_programmes import filter_sections_for_program
from core.services.section_snapshot_guard import section_snapshot_operation_guard
from core.services.student_sections import (
    replace_student_term_sections,
    section_is_available_to_student,
)
from core.services.term_sections import (
    import_term_sections_from_csv,
)
from core.services.term_sections import (
    preview_term_sections_from_csv as _preview_term_sections_from_csv,
)

pytestmark = pytest.mark.django_db


class _PreviewRow(TypedDict, total=False):
    programs: list[str]
    programme_source: str
    requested_programs: list[str]
    effective_programs: list[str]
    effective_program_assignments: list[dict[str, str]]
    day: str
    start_time: str


class _PreviewImpact(TypedDict, total=False):
    sections_unique: int
    meeting_rows_unique: int
    sections_new: int
    sections_existing: int
    programme_assignments_effective: int
    membership_adds: int
    membership_removes: int
    membership_promotions: int
    membership_source_changes: int
    predicted_fully_unassigned_sections: int
    fully_unassigned_sections: list[dict[str, object]]


class _PreviewResult(TypedDict, total=False):
    preview_rows: list[_PreviewRow]
    impact: _PreviewImpact
    has_program_column: bool
    unassigned_section_count: int
    program_membership_warning: str
    can_import: bool
    expected_confirmation: str
    default_programs: list[str]
    preview_fingerprint: str


def preview_term_sections_from_csv(
    csv_path: str | Path,
    academic_year: str = "",
    term: str = "",
    source_tag: str = "other",
    max_preview_rows: int = 300,
    default_programs: list[object] | None = None,
) -> _PreviewResult:
    """Give tests the concrete subset of the preview contract they assert."""
    return cast(
        _PreviewResult,
        _preview_term_sections_from_csv(
            csv_path,
            academic_year=academic_year,
            term=term,
            source_tag=source_tag,
            max_preview_rows=max_preview_rows,
            default_programs=default_programs,
        ),
    )


@pytest.fixture(autouse=True)
def _known_programme_codes() -> None:
    for index, program in enumerate(("AI", "AI2", "DS"), start=1):
        ProgrammeRequirement.objects.create(
            program=program,
            course_code=f"KNOWN{index}",
        )


def _section(
    section: str,
    *,
    course_key: str = "CS112",
    scenario: TimetableScenario | None = None,
) -> TermSection:
    return TermSection.objects.create(
        scenario=scenario,
        course_code="CS",
        course_number="112",
        course_key=course_key,
        course_name="Programming II",
        section=section,
    )


def _write_section_csv(
    path: Path,
    *,
    include_programmes: bool,
    programmes: str = " ai, DS|AI2;ai ",
) -> None:
    fields = [
        "course_name",
        "course_code",
        "course_number",
        "section",
        "available_capacity",
        "registered_count",
        "day",
        "start_time",
        "end_time",
        "building",
        "floor_wing",
        "room",
        "instructor",
    ]
    if include_programmes:
        fields.append("programmes")

    row = {
        "course_name": "Programming II",
        "course_code": "CS",
        "course_number": "112",
        "section": "M1",
        "day": "SUN",
        "start_time": "09:00",
        "end_time": "10:15",
        "building": "B1",
        "floor_wing": "1",
        "room": "101",
        "instructor": "Dr Test",
    }
    if include_programmes:
        row["programmes"] = programmes

    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerow(row)


def _write_custom_section_csv(
    path: Path,
    rows: list[dict[str, str]],
    *,
    rich: bool,
) -> None:
    fields = [
        "course_name",
        "course_code",
        "course_number",
        "section",
        "available_capacity",
        "registered_count",
        "day",
        "start_time",
        "end_time",
        "building",
        "floor_wing",
        "room",
        "instructor",
    ]
    if rich:
        fields.append("programs")
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _csv_row(
    *,
    course_number: str,
    section: str,
    day: str,
    programs: str = "",
) -> dict[str, str]:
    return {
        "course_name": f"Course {course_number}",
        "course_code": "CS",
        "course_number": course_number,
        "section": section,
        "day": day,
        "start_time": "09:00",
        "end_time": "10:15",
        "building": "B1",
        "floor_wing": "1",
        "room": "101",
        "instructor": "Dr Test",
        "programs": programs,
    }


def test_programme_membership_is_normalized_and_filters_exactly() -> None:
    ai = _section("M1")
    ds = _section("M2")
    unassigned = _section("M3")
    TermSectionProgram.objects.create(term_section=ai, program=" ai ")
    TermSectionProgram.objects.create(term_section=ds, program="DS")

    assert TermSectionProgram.objects.get(term_section=ai).program == "AI"
    assert set(filter_sections_for_program(TermSection.objects.all(), " ai ")) == {ai}
    assert set(
        filter_sections_for_program(
            TermSection.objects.all(),
            "AI",
            include_unassigned=True,
        )
    ) == {ai, unassigned}
    assert not filter_sections_for_program(TermSection.objects.all(), "").exists()


def test_data_migration_backfills_only_global_observed_memberships() -> None:
    student = Student.objects.create(
        student_id=910000,
        name="Student",
        program=" ai ",
        section="M",
    )
    global_section = _section("M1")
    scenario = TimetableScenario.objects.create(academic_year="1448", term="1", name="Draft")
    scenario_owned = _section("M2", scenario=scenario)
    for section in (global_section, scenario_owned):
        StudentTermSection.objects.create(
            student_id=student.student_id,
            academic_year="1448",
            term="1",
            term_section=section,
        )

    migration = importlib.import_module("core.migrations.0059_termsectionprogram")
    migration.backfill_observed_program_memberships(
        apps,
        SimpleNamespace(connection=connection),
    )

    assert set(global_section.program_links.values_list("program", "assignment_source")) == {
        ("AI", "observed")
    }
    assert not scenario_owned.program_links.exists()


def test_replacing_student_sections_reconciles_only_observed_global_links() -> None:
    ai_student = Student.objects.create(
        student_id=910001,
        name="AI Student",
        program=" ai ",
        section="M",
    )
    ds_student = Student.objects.create(
        student_id=910002,
        name="DS Student",
        program="DS",
        section="M",
    )
    old = _section("M1")
    replacement = _section("M2")
    scenario = TimetableScenario.objects.create(academic_year="1448", term="1", name="Draft")
    scenario_owned = _section("M3", scenario=scenario)

    for student in (ai_student, ds_student):
        StudentTermSection.objects.create(
            student_id=student.student_id,
            academic_year="1448",
            term="1",
            term_section=old,
        )
    TermSectionProgram.objects.create(
        term_section=old,
        program="AI",
        assignment_source="import",
    )
    TermSectionProgram.objects.create(term_section=old, program="DS")
    TermSectionProgram.objects.create(term_section=old, program="STALE")

    replace_student_term_sections(
        ai_student.student_id,
        "1448",
        "1",
        [replacement.id, scenario_owned.id],
        source="scraper_timetable",
    )

    assert set(old.program_links.values_list("program", "assignment_source")) == {
        ("AI", "import"),
        ("DS", "observed"),
    }
    assert set(replacement.program_links.values_list("program", "assignment_source")) == {
        ("AI", "observed")
    }
    assert not scenario_owned.program_links.exists()


def test_student_availability_requires_programme_membership_for_global_sections() -> None:
    student = Student.objects.create(
        student_id=910003,
        name="AI Student",
        program="AI",
        section="M",
    )
    assigned = _section("M1")
    unassigned = _section("M2")
    other_program = _section("M3")
    wrong_gender = _section("F1")
    scenario = TimetableScenario.objects.create(academic_year="1448", term="1", name="Draft")
    scenario_owned = _section("M4", scenario=scenario)
    TermSectionProgram.objects.create(term_section=assigned, program="AI")
    TermSectionProgram.objects.create(term_section=other_program, program="DS")
    TermSectionProgram.objects.create(term_section=wrong_gender, program="AI")

    assert section_is_available_to_student(assigned, student_id=student.student_id)
    assert not section_is_available_to_student(unassigned, student_id=student.student_id)
    assert not section_is_available_to_student(other_program, student_id=student.student_id)
    assert not section_is_available_to_student(wrong_gender, student_id=student.student_id)
    assert section_is_available_to_student(scenario_owned, student_id=student.student_id)


def test_student_availability_fails_closed_when_programme_is_blank() -> None:
    student = Student.objects.create(
        student_id=910004,
        name="Incomplete Student",
        program="  ",
        section="M",
    )
    section = _section("M1")
    TermSectionProgram.objects.create(term_section=section, program="AI")

    assert not section_is_available_to_student(section, student_id=student.student_id)


def test_student_catalog_save_and_chat_refuse_a_blank_programme() -> None:
    student = Student.objects.create(
        student_id=910005,
        name="Incomplete Student",
        program="",
        section="M",
    )
    section = _section("M1")
    TermSectionProgram.objects.create(term_section=section, program="AI")

    ensure_role_groups()
    user = User.objects.create_user(username="programme-admin", password="test-password")
    user.groups.add(Group.objects.get(name=ROLE_SUPER_ADMIN))
    client = Client()
    client.force_login(user)

    catalog = client.post(
        "/ops/planner/sections-catalog/",
        data=json.dumps(
            {
                "academic_year": "1448",
                "term": "1",
                "student_id": student.student_id,
                "course_codes": ["CS112"],
            }
        ),
        content_type="application/json",
    )
    assert catalog.status_code == 409
    assert catalog.json()["error"]["code"] == "STUDENT_PROGRAM_UNRESOLVED"

    save = client.post(
        "/ops/planner/save-student-sections/",
        data=json.dumps(
            {
                "student_id": student.student_id,
                "academic_year": "1448",
                "term": "1",
                "term_section_ids": [section.id],
                "confirm_replace": True,
            }
        ),
        content_type="application/json",
    )
    assert save.status_code == 409
    assert save.json()["error"]["code"] == "SECTION_NOT_AVAILABLE_TO_STUDENT"
    assert not StudentTermSection.objects.filter(student_id=student.student_id).exists()

    from core.services.virtual_advisor_capabilities import _student_sections_context

    error, context = _student_sections_context(
        {
            "student_id": student.student_id,
            "academic_year": 1448,
            "term": 1,
            "course_code": "CS112",
        },
        {"role": ROLE_SUPER_ADMIN},
        {},
    )
    assert context == {}
    assert error is not None
    assert error["reason"] == "PROGRAMME_UNRESOLVED"


def test_csv_preview_and_import_support_multi_programme_membership(tmp_path: Path) -> None:
    section = _section("M1")
    TermSectionProgram.objects.create(
        term_section=section,
        program="DS",
        assignment_source="observed",
    )
    csv_path = tmp_path / "sections.csv"
    _write_section_csv(csv_path, include_programmes=True)

    preview = preview_term_sections_from_csv(csv_path)
    assert preview["preview_rows"][0]["programs"] == ["AI", "AI2", "DS"]

    result = import_term_sections_from_csv(csv_path, truncate_existing_term=False)
    assert result["program_links_upserted"] == 3
    assert set(section.program_links.values_list("program", "assignment_source")) == {
        ("AI", "import"),
        ("AI2", "import"),
        ("DS", "import"),
    }

    # A legacy CSV with no programme column still imports and does not erase
    # memberships learned through registrations or an earlier richer import.
    legacy_path = tmp_path / "legacy_sections.csv"
    _write_section_csv(legacy_path, include_programmes=False)
    legacy_preview = preview_term_sections_from_csv(legacy_path)
    assert legacy_preview["preview_rows"][0]["programs"] == []
    assert legacy_preview["preview_rows"][0]["programme_source"] == "preserved"
    assert legacy_preview["preview_rows"][0]["requested_programs"] == []
    assert legacy_preview["preview_rows"][0]["effective_programs"] == ["AI", "AI2", "DS"]
    assert legacy_preview["impact"]["programme_assignments_effective"] == 3
    assert legacy_preview["has_program_column"] is False
    assert legacy_preview["unassigned_section_count"] == 0
    assert legacy_preview["program_membership_warning"]
    import_term_sections_from_csv(legacy_path, truncate_existing_term=False)
    assert set(section.program_links.values_list("program", flat=True)) == {"AI", "AI2", "DS"}


def test_truncating_import_is_refused_before_existing_sections_are_deleted(
    tmp_path: Path,
) -> None:
    section = _section("M1")
    meeting = TermSectionMeeting.objects.create(
        term_section=section,
        day="MON",
        start_time="10:30",
        end_time="11:45",
    )
    csv_path = tmp_path / "legacy_sections.csv"
    _write_section_csv(csv_path, include_programmes=False)

    with pytest.raises(ValueError, match="Clear Current Section Snapshot"):
        import_term_sections_from_csv(csv_path, truncate_existing_term=True)

    assert TermSection.objects.filter(pk=section.pk).exists()
    assert TermSectionMeeting.objects.filter(pk=meeting.pk).exists()


def test_programme_aware_merge_replaces_import_links_and_rebuilds_observed_links(
    tmp_path: Path,
) -> None:
    section = _section("M1")
    student = Student.objects.create(
        student_id=910006,
        name="DS Student",
        program="DS",
        section="M",
    )
    StudentTermSection.objects.create(
        student_id=student.student_id,
        academic_year="1448",
        term="1",
        term_section=section,
    )
    TermSectionProgram.objects.create(
        term_section=section,
        program="AI",
        assignment_source="import",
    )
    TermSectionProgram.objects.create(
        term_section=section,
        program="DS",
        assignment_source="import",
    )

    csv_path = tmp_path / "sections.csv"
    _write_section_csv(csv_path, include_programmes=True, programmes="AI")
    result = import_term_sections_from_csv(csv_path, truncate_existing_term=False)

    assert result["program_links_removed"] == 1
    assert set(section.program_links.values_list("program", "assignment_source")) == {
        ("AI", "import"),
        ("DS", "observed"),
    }

    # A present but blank programme cell explicitly removes imported ownership;
    # the real DS registration remains authoritative observed evidence.
    _write_section_csv(csv_path, include_programmes=True, programmes="")
    blank_result = import_term_sections_from_csv(csv_path, truncate_existing_term=False)
    assert blank_result["unassigned_section_count"] == 0
    assert set(section.program_links.values_list("program", "assignment_source")) == {
        ("DS", "observed")
    }


def test_new_legacy_section_requires_default_programmes(tmp_path: Path) -> None:
    csv_path = tmp_path / "oracle_sections.csv"
    _write_section_csv(csv_path, include_programmes=False)

    preview = preview_term_sections_from_csv(csv_path)
    assert preview["can_import"] is False
    assert preview["preview_rows"][0]["programme_source"] == "unassigned"
    assert preview["expected_confirmation"] == "IMPORT 1"
    assert preview["impact"] == {
        "sections_unique": 1,
        "meeting_rows_unique": 1,
        "sections_new": 1,
        "sections_existing": 0,
        "programme_assignments_effective": 0,
        "membership_adds": 0,
        "membership_removes": 0,
        "membership_promotions": 0,
        "membership_source_changes": 0,
        "predicted_fully_unassigned_sections": 1,
        "fully_unassigned_sections": [{"course_key": "CS112", "section": "M1", "existing": False}],
    }

    with pytest.raises(ValueError, match="without a programme membership"):
        import_term_sections_from_csv(csv_path)
    assert not TermSection.objects.filter(course_key="CS112", section="M1").exists()


def test_empty_section_csv_is_refused_in_preview_and_import(tmp_path: Path) -> None:
    csv_path = tmp_path / "empty.csv"
    _write_custom_section_csv(csv_path, [], rich=False)

    with pytest.raises(ValueError, match="no section rows"):
        preview_term_sections_from_csv(csv_path, default_programs=["AI"])
    with pytest.raises(ValueError, match="no section rows"):
        import_term_sections_from_csv(csv_path, default_programs=["AI"])


def test_defaults_create_multiple_memberships_and_rich_csv_overrides_them(
    tmp_path: Path,
) -> None:
    legacy_path = tmp_path / "oracle_sections.csv"
    _write_section_csv(legacy_path, include_programmes=False)

    preview = preview_term_sections_from_csv(
        legacy_path,
        default_programs=[" ds ", "AI", "ai"],
    )
    assert preview["default_programs"] == ["AI", "DS"]
    assert preview["preview_rows"][0]["effective_programs"] == ["AI", "DS"]
    assert preview["preview_rows"][0]["programme_source"] == "default"
    assert preview["impact"]["programme_assignments_effective"] == 2
    assert preview["impact"]["membership_adds"] == 2
    import_term_sections_from_csv(
        legacy_path,
        default_programs=["DS", "AI"],
    )
    section = TermSection.objects.get(course_key="CS112", section="M1")
    assert set(section.program_links.values_list("program", flat=True)) == {"AI", "DS"}

    rich_path = tmp_path / "rich_sections.csv"
    _write_section_csv(rich_path, include_programmes=True, programmes="AI2")
    rich_preview = preview_term_sections_from_csv(
        rich_path,
        default_programs=["DS"],
    )
    assert rich_preview["preview_rows"][0]["effective_programs"] == ["AI2"]
    assert rich_preview["preview_rows"][0]["programme_source"] == "csv"
    import_term_sections_from_csv(rich_path, default_programs=["DS"])
    assert set(section.program_links.values_list("program", flat=True)) == {"AI2"}


def test_section_import_refuses_while_a_snapshot_operation_holds_the_guard(
    tmp_path: Path,
) -> None:
    csv_path = tmp_path / "oracle_sections.csv"
    _write_section_csv(csv_path, include_programmes=False)

    with section_snapshot_operation_guard(blocking=False) as acquired:
        assert acquired is True
        with pytest.raises(ValueError, match="retry preview shortly"):
            preview_term_sections_from_csv(csv_path, default_programs=["AI"])
        with pytest.raises(ValueError, match="operation is in progress"):
            import_term_sections_from_csv(csv_path, default_programs=["AI"])

    assert not TermSection.objects.filter(course_key="CS112", section="M1").exists()


def test_explicit_blank_without_defaults_is_blocked_unless_observed_keeps_assignment(
    tmp_path: Path,
) -> None:
    csv_path = tmp_path / "blank_programmes.csv"
    _write_section_csv(csv_path, include_programmes=True, programmes="")

    blocked = preview_term_sections_from_csv(csv_path)
    assert blocked["can_import"] is False
    assert blocked["impact"]["predicted_fully_unassigned_sections"] == 1
    with pytest.raises(ValueError, match="without a programme membership"):
        import_term_sections_from_csv(csv_path)

    section = _section("M1")
    student = Student.objects.create(
        student_id=910007,
        name="Observed Student",
        program="DS",
        section="M",
    )
    StudentTermSection.objects.create(
        student_id=student.student_id,
        academic_year="1448",
        term="1",
        term_section=section,
    )
    allowed = preview_term_sections_from_csv(csv_path)
    assert allowed["can_import"] is True
    assert allowed["preview_rows"][0]["programme_source"] == "preserved"
    assert allowed["preview_rows"][0]["requested_programs"] == []
    assert allowed["preview_rows"][0]["effective_programs"] == ["DS"]
    assert allowed["impact"]["predicted_fully_unassigned_sections"] == 0
    import_term_sections_from_csv(csv_path)
    assert set(section.program_links.values_list("program", "assignment_source")) == {
        ("DS", "observed")
    }


def test_preview_impact_counts_match_the_applied_merge(tmp_path: Path) -> None:
    existing = _section("M1")
    TermSectionProgram.objects.create(
        term_section=existing,
        program="AI",
        assignment_source="observed",
    )
    csv_path = tmp_path / "impact.csv"
    rows = [
        _csv_row(course_number="112", section="M1", day="SUN"),
        _csv_row(course_number="112", section="M1", day="SUN"),
        _csv_row(course_number="113", section="M2", day="MON"),
    ]
    _write_custom_section_csv(csv_path, rows, rich=False)

    preview = preview_term_sections_from_csv(
        csv_path,
        default_programs=["AI", "DS"],
    )
    impact = preview["impact"]
    assert impact["sections_unique"] == 2
    assert impact["meeting_rows_unique"] == 2
    assert impact["sections_existing"] == 1
    assert impact["sections_new"] == 1
    assert impact["programme_assignments_effective"] == 4
    assert impact["membership_adds"] == 3
    assert impact["membership_promotions"] == 1
    assert impact["predicted_fully_unassigned_sections"] == 0

    result = import_term_sections_from_csv(
        csv_path,
        default_programs=["DS", "AI"],
        expected_preview_fingerprint=preview["preview_fingerprint"],
    )
    assert result["preview_fingerprint"] == preview["preview_fingerprint"]
    assert result["impact"] == impact
    assert result["sections_imported"] == impact["sections_unique"]
    assert result["inserted_or_updated"] == impact["meeting_rows_unique"]
    assert (
        TermSectionProgram.objects.filter(term_section__course_key__in=["CS112", "CS113"]).count()
        == impact["programme_assignments_effective"]
    )


def test_import_refuses_a_file_changed_after_preview_without_writing(tmp_path: Path) -> None:
    csv_path = tmp_path / "sections.csv"
    _write_section_csv(csv_path, include_programmes=False)
    preview = preview_term_sections_from_csv(
        csv_path,
        source_tag="other",
        default_programs=["AI"],
    )
    fingerprint = preview["preview_fingerprint"]
    assert isinstance(fingerprint, str) and len(fingerprint) == 64
    assert (
        preview_term_sections_from_csv(
            csv_path,
            source_tag="other",
            default_programs=[" ai "],
        )["preview_fingerprint"]
        == fingerprint
    )
    assert (
        preview_term_sections_from_csv(
            csv_path,
            source_tag="department",
            default_programs=["AI"],
        )["preview_fingerprint"]
        != fingerprint
    )

    changed_row = _csv_row(course_number="112", section="M1", day="MON")
    _write_custom_section_csv(csv_path, [changed_row], rich=False)
    with pytest.raises(ValueError, match="preview is stale"):
        import_term_sections_from_csv(
            csv_path,
            source_tag="other",
            default_programs=["AI"],
            expected_preview_fingerprint=fingerprint,
        )

    assert not TermSection.objects.filter(course_key="CS112", section="M1").exists()


def test_section_csv_rejects_inconsistent_repeated_section_metadata(tmp_path: Path) -> None:
    csv_path = tmp_path / "inconsistent-section.csv"
    first = _csv_row(course_number="112", section="M1", day="SUN")
    second = _csv_row(course_number="112", section="M1", day="MON")
    second["course_name"] = "A conflicting course name"
    _write_custom_section_csv(csv_path, [first, second], rich=False)

    with pytest.raises(
        ValueError,
        match=r"Inconsistent section metadata.*course_name",
    ):
        preview_term_sections_from_csv(csv_path, default_programs=["AI"])
    with pytest.raises(ValueError, match="Inconsistent section metadata"):
        import_term_sections_from_csv(csv_path, default_programs=["AI"])

    assert not TermSection.objects.filter(course_key="CS112", section="M1").exists()


def test_section_csv_rejects_conflicting_duplicate_meeting_location(tmp_path: Path) -> None:
    csv_path = tmp_path / "inconsistent-meeting.csv"
    first = _csv_row(course_number="112", section="M1", day="SUN")
    second = dict(first)
    second["building"] = "B2"
    second["floor_wing"] = "2"
    _write_custom_section_csv(csv_path, [first, second], rich=False)

    with pytest.raises(ValueError, match="Inconsistent meeting location"):
        preview_term_sections_from_csv(csv_path, default_programs=["AI"])


def test_import_refuses_reordered_rows_after_preview(tmp_path: Path) -> None:
    csv_path = tmp_path / "sections.csv"
    sunday = _csv_row(course_number="112", section="M1", day="SUN")
    monday = _csv_row(course_number="112", section="M1", day="MON")
    _write_custom_section_csv(csv_path, [sunday, monday], rich=False)
    preview = preview_term_sections_from_csv(csv_path, default_programs=["AI"])

    _write_custom_section_csv(csv_path, [monday, sunday], rich=False)
    with pytest.raises(ValueError, match="preview is stale"):
        import_term_sections_from_csv(
            csv_path,
            default_programs=["AI"],
            expected_preview_fingerprint=preview["preview_fingerprint"],
        )

    assert not TermSection.objects.filter(course_key="CS112", section="M1").exists()


def test_import_fingerprint_is_bound_to_the_resolved_source_path(tmp_path: Path) -> None:
    preview_path = tmp_path / "preview.csv"
    import_path = tmp_path / "import.csv"
    _write_section_csv(preview_path, include_programmes=False)
    _write_section_csv(import_path, include_programmes=False)
    preview = preview_term_sections_from_csv(preview_path, default_programs=["AI"])

    with pytest.raises(ValueError, match="preview is stale"):
        import_term_sections_from_csv(
            import_path,
            default_programs=["AI"],
            expected_preview_fingerprint=preview["preview_fingerprint"],
        )

    assert not TermSection.objects.filter(course_key="CS112", section="M1").exists()


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"course_code": ""}, "course_code and course_number"),
        ({"course_number": ""}, "course_code and course_number"),
        ({"section": ""}, "section must be nonblank"),
        ({"day": "FRI"}, "day must be one of"),
        ({"start_time": "nine"}, "start_time must use valid HH:MM"),
        ({"end_time": "24:00"}, "end_time must use valid HH:MM"),
        ({"start_time": "11:00", "end_time": "10:00"}, "start_time must be earlier"),
        ({"available_capacity": "many"}, "available_capacity must be a non-negative"),
        ({"registered_count": "-1"}, "registered_count must be a non-negative"),
    ],
)
def test_section_csv_reports_row_number_for_invalid_values(
    tmp_path: Path,
    changes: dict[str, str],
    message: str,
) -> None:
    csv_path = tmp_path / "invalid.csv"
    row = _csv_row(course_number="112", section="M1", day="SUN")
    row.update(changes)
    _write_custom_section_csv(csv_path, [row], rich=False)

    with pytest.raises(ValueError, match=rf"CSV row 2.*{message}"):
        preview_term_sections_from_csv(csv_path, default_programs=["AI"])


def test_section_csv_normalizes_single_digit_hour_before_validation(tmp_path: Path) -> None:
    csv_path = tmp_path / "single-digit-hour.csv"
    row = _csv_row(course_number="112", section="M1", day="sun")
    row["start_time"] = "9:00"
    _write_custom_section_csv(csv_path, [row], rich=False)

    preview = preview_term_sections_from_csv(csv_path, default_programs=["AI"])

    assert preview["preview_rows"][0]["day"] == "SUN"
    assert preview["preview_rows"][0]["start_time"] == "09:00"


def test_section_csv_rejects_unknown_default_and_csv_programmes(tmp_path: Path) -> None:
    legacy_path = tmp_path / "legacy.csv"
    _write_section_csv(legacy_path, include_programmes=False)
    with pytest.raises(ValueError, match=r"Unknown programme code\(s\): UNKNOWN"):
        preview_term_sections_from_csv(legacy_path, default_programs=["unknown"])

    rich_path = tmp_path / "rich.csv"
    _write_section_csv(rich_path, include_programmes=True, programmes="AI,UNKNOWN")
    with pytest.raises(ValueError, match=r"Unknown programme code\(s\): UNKNOWN"):
        import_term_sections_from_csv(rich_path)

    assert not TermSection.objects.filter(course_key="CS112", section="M1").exists()


def test_existing_section_membership_makes_programme_code_known(tmp_path: Path) -> None:
    known_section = _section("M9", course_key="CS999")
    TermSectionProgram.objects.create(
        term_section=known_section,
        program="LEGACY",
        assignment_source="manual",
    )
    csv_path = tmp_path / "legacy-programme.csv"
    row = _csv_row(course_number="113", section="M2", day="MON")
    _write_custom_section_csv(csv_path, [row], rich=False)

    preview = preview_term_sections_from_csv(csv_path, default_programs=["legacy"])
    result = import_term_sections_from_csv(
        csv_path,
        default_programs=["LEGACY"],
        expected_preview_fingerprint=preview["preview_fingerprint"],
    )

    assert result["can_import"] is True
    imported = TermSection.objects.get(course_key="CS113", section="M2")
    assert set(imported.program_links.values_list("program", flat=True)) == {"LEGACY"}


def test_preview_exposes_mixed_effective_programme_assignments(tmp_path: Path) -> None:
    section = _section("M1")
    TermSectionProgram.objects.create(
        term_section=section,
        program="DS",
        assignment_source="manual",
    )
    csv_path = tmp_path / "mixed.csv"
    _write_section_csv(csv_path, include_programmes=True, programmes="AI")

    preview = preview_term_sections_from_csv(csv_path)
    row = preview["preview_rows"][0]

    assert row["programme_source"] == "mixed"
    assert row["effective_programs"] == ["AI", "DS"]
    assert row["effective_program_assignments"] == [
        {"program": "AI", "assignment_source": "import"},
        {"program": "DS", "assignment_source": "manual"},
    ]


def test_guarded_backup_runs_outside_atomic_then_import_revalidates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    section = _section("M1")
    TermSectionProgram.objects.create(
        term_section=section,
        program="AI",
        assignment_source="import",
    )
    csv_path = tmp_path / "sections.csv"
    _write_section_csv(csv_path, include_programmes=True, programmes="AI")
    preview = preview_term_sections_from_csv(csv_path)
    baseline_atomic_depth = len(connection.atomic_blocks)

    def fake_backup() -> dict[str, object]:
        assert len(connection.atomic_blocks) == baseline_atomic_depth
        TermSectionMeeting.objects.create(
            term_section=section,
            day="MON",
            start_time="13:00",
            end_time="14:15",
            room="202",
        )
        return {"ok": True, "backup_file": "test.sqlite3"}

    monkeypatch.setattr(
        "core.services.db_admin_ops.create_backup_snapshot",
        fake_backup,
    )
    with pytest.raises(ValueError, match="preview is stale"):
        import_term_sections_from_csv(
            csv_path,
            expected_preview_fingerprint=preview["preview_fingerprint"],
            backup_before_import=True,
        )

    assert list(section.meetings.values_list("day", flat=True)) == ["MON"]


def test_guarded_backup_result_is_returned_and_failure_prevents_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    csv_path = tmp_path / "sections.csv"
    _write_section_csv(csv_path, include_programmes=False)
    preview = preview_term_sections_from_csv(csv_path, default_programs=["AI"])
    backup = {"ok": True, "backup_file": "test.sqlite3", "size_bytes": 123}
    monkeypatch.setattr(
        "core.services.db_admin_ops.create_backup_snapshot",
        lambda: backup,
    )

    result = import_term_sections_from_csv(
        csv_path,
        default_programs=["AI"],
        expected_preview_fingerprint=preview["preview_fingerprint"],
        backup_before_import=True,
    )
    assert result["backup"] == backup

    second_path = tmp_path / "second.csv"
    row = _csv_row(course_number="113", section="M2", day="MON")
    _write_custom_section_csv(second_path, [row], rich=False)
    second_preview = preview_term_sections_from_csv(second_path, default_programs=["AI"])
    monkeypatch.setattr(
        "core.services.db_admin_ops.create_backup_snapshot",
        lambda: {"ok": False},
    )
    with pytest.raises(RuntimeError, match="backup failed"):
        import_term_sections_from_csv(
            second_path,
            default_programs=["AI"],
            expected_preview_fingerprint=second_preview["preview_fingerprint"],
            backup_before_import=True,
        )
    assert not TermSection.objects.filter(course_key="CS113", section="M2").exists()


def test_import_refuses_affected_membership_and_meeting_drift_atomically(
    tmp_path: Path,
) -> None:
    section = _section("M1")
    TermSectionProgram.objects.create(
        term_section=section,
        program="AI",
        assignment_source="import",
    )
    csv_path = tmp_path / "sections.csv"
    _write_section_csv(csv_path, include_programmes=True, programmes="AI")
    preview = preview_term_sections_from_csv(csv_path)

    drift_meeting = TermSectionMeeting.objects.create(
        term_section=section,
        day="MON",
        start_time="13:00",
        end_time="14:15",
        room="202",
    )
    TermSectionProgram.objects.create(
        term_section=section,
        program="DS",
        assignment_source="import",
    )

    with pytest.raises(ValueError, match="preview is stale"):
        import_term_sections_from_csv(
            csv_path,
            expected_preview_fingerprint=preview["preview_fingerprint"],
        )

    assert list(section.meetings.values_list("id", flat=True)) == [drift_meeting.id]
    assert set(section.program_links.values_list("program", "assignment_source")) == {
        ("AI", "import"),
        ("DS", "import"),
    }


def _staff_planner_client(username: str) -> Client:
    ensure_role_groups()
    user = User.objects.create_user(username=username, password="test-password")
    user.groups.add(Group.objects.get(name=ROLE_SUPER_ADMIN))
    client = Client()
    client.force_login(user)
    return client


def test_planner_programme_only_flag_controls_catalogue_scope() -> None:
    student = Student.objects.create(
        student_id=910006,
        name="AI Student",
        program="AI",
        section="M",
    )
    ai_only = _section("M1")
    shared = _section("M2")
    ds_only = _section("M3")
    unassigned = _section("M4")
    wrong_gender = _section("F1")
    TermSectionProgram.objects.create(term_section=ai_only, program="AI")
    TermSectionProgram.objects.create(term_section=shared, program="AI")
    TermSectionProgram.objects.create(term_section=shared, program="DS")
    TermSectionProgram.objects.create(term_section=ds_only, program="DS")
    TermSectionProgram.objects.create(term_section=wrong_gender, program="AI")
    client = _staff_planner_client("planner-programme-scope")

    def catalog(program_sections_only: bool) -> set[int]:
        response = client.post(
            "/ops/planner/sections-catalog/",
            data=json.dumps(
                {
                    "academic_year": "1448",
                    "term": "1",
                    "student_id": student.student_id,
                    "course_codes": ["CS112"],
                    "program_sections_only": program_sections_only,
                }
            ),
            content_type="application/json",
        )
        assert response.status_code == 200
        assert response.json()["program_sections_only"] is program_sections_only
        return {int(row["term_section_id"]) for row in response.json()["sections"]}

    assert catalog(True) == {ai_only.id, shared.id}
    assert catalog(False) == {ai_only.id, shared.id, ds_only.id, unassigned.id}
    assert wrong_gender.id not in catalog(False)


def test_planner_build_maps_controls_to_programme_and_capacity_contracts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    student = Student.objects.create(
        student_id=910007,
        name="AI Student",
        program="AI",
        section="M",
    )
    calls: list[dict[str, object]] = []

    def fake_build_plans(*args: object, **kwargs: object) -> dict[str, object]:
        calls.append(kwargs)
        return {"summary": {}, "options": [], "swap_suggestions": []}

    monkeypatch.setattr("core.planner_views.build_plans", fake_build_plans)
    client = _staff_planner_client("planner-control-contract")

    def build(program_only: bool, allow_full: bool) -> dict[str, object]:
        response = client.post(
            "/ops/planner/build/",
            data=json.dumps(
                {
                    "student_id": student.student_id,
                    "academic_year": "1448",
                    "term": "1",
                    "mode": "keep",
                    "shortlist": [{"course_code": "CS112", "credits": 3, "status": "Eligible"}],
                    "baseline": [],
                    "program_sections_only": program_only,
                    "allow_full_sections": allow_full,
                    "max_credits": 18,
                }
            ),
            content_type="application/json",
        )
        assert response.status_code == 200
        return cast(dict[str, object], response.json())

    strict_result = build(True, False)
    relaxed_result = build(False, True)

    assert calls[0]["program"] == "AI"
    assert calls[0]["strict_per_course"] is False
    assert calls[0]["consider_capacity"] is True
    assert strict_result["constraints"] == {
        "program_sections_only": True,
        "allow_full_sections": False,
    }

    assert calls[1]["program"] is None
    assert calls[1]["strict_per_course"] is False
    assert calls[1]["consider_capacity"] is False
    assert relaxed_result["constraints"] == {
        "program_sections_only": False,
        "allow_full_sections": True,
    }


def test_planner_build_preserves_boolean_must_take_and_one_exact_pin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    student = Student.objects.create(
        student_id=910008,
        name="Pinned Student",
        program="AI",
        section="M",
    )
    captured_shortlists: list[list[dict[str, object]]] = []

    def fake_build_plans(*args: object, **kwargs: object) -> dict[str, object]:
        captured_shortlists.append(cast(list[dict[str, object]], args[2]))
        return {"summary": {}, "options": [], "swap_suggestions": []}

    monkeypatch.setattr("core.planner_views.build_plans", fake_build_plans)
    client = _staff_planner_client("planner-hard-request-contract")
    response = client.post(
        "/ops/planner/build/",
        data=json.dumps(
            {
                "student_id": student.student_id,
                "academic_year": "1448",
                "term": "1",
                "mode": "keep",
                "program_sections_only": False,
                "shortlist": [
                    {
                        "course_code": "engl214",
                        "credits": 3,
                        "status": "Eligible",
                        "must_take": True,
                        "pinned_sections": [{"term_section_id": 123, "section": "M2"}],
                    }
                ],
                "baseline": [],
            }
        ),
        content_type="application/json",
    )

    assert response.status_code == 200
    assert captured_shortlists == [
        [
            {
                "course_code": "ENGL214",
                "priority": "Med",
                "score": 0,
                "status": "Eligible",
                "missing_prerequisites": [],
                "must_take": True,
                "credits": 3,
                "pinned_sections": [{"term_section_id": 123, "section": "M2"}],
            }
        ]
    ]


@pytest.mark.parametrize(
    ("must_take", "pins", "error_code"),
    [
        ("false", [], "VALIDATION_MUST_TAKE"),
        (
            False,
            [
                {"term_section_id": 123, "section": "M2"},
                {"term_section_id": 124, "section": "M3"},
            ],
            "VALIDATION_PINNED_SECTION_COUNT",
        ),
    ],
)
def test_planner_build_rejects_ambiguous_hard_request_payloads(
    must_take: object,
    pins: list[dict[str, object]],
    error_code: str,
) -> None:
    student = Student.objects.create(
        student_id=910009,
        name="Validation Student",
        program="AI",
        section="M",
    )
    client = _staff_planner_client(f"planner-hard-validation-{error_code.lower()}")
    response = client.post(
        "/ops/planner/build/",
        data=json.dumps(
            {
                "student_id": student.student_id,
                "academic_year": "1448",
                "term": "1",
                "mode": "keep",
                "program_sections_only": False,
                "shortlist": [
                    {
                        "course_code": "ENGL214",
                        "credits": 3,
                        "status": "Eligible",
                        "must_take": must_take,
                        "pinned_sections": pins,
                    }
                ],
                "baseline": [],
            }
        ),
        content_type="application/json",
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == error_code


def test_staff_planner_ui_uses_clear_positive_control_names() -> None:
    root = Path(__file__).resolve().parents[1]
    template = (root / "core" / "templates" / "core" / "planner.html").read_text(encoding="utf-8")
    script = (root / "static" / "js" / "page-planner.js").read_text(encoding="utf-8")

    assert 'id="programSectionsOnly" type="checkbox" checked' in template
    assert 'id="allowFullSections" type="checkbox"' in template
    assert "Student programme sections only" in template
    assert "Allow full sections" in template
    assert "program_sections_only:useProgrammeSectionsOnly()" in script
    assert "allow_full_sections:allowFullSections()" in script
    assert "Must-take — every result" in script
    assert "existing.pinned_sections=[{term_section_id:tsid" in script
    assert "function renderShortlist(){\n  invalidateBuilderResults();" in script
    assert "hard_constraint_failures" in script
    assert "q('strictSections')" not in script
    assert "q('ignoreCapacity')" not in script
