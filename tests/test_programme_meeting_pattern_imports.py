"""A programme's weekly-meetings vote across catalogue writes (PR 2, design §8).

``ProgrammeRequirement.meeting_pattern`` is set only in Section Planning. The
study-plan importers rewrite the rows that carry it; each one snapshots the
votes in scope, clears them, runs unchanged, and puts a vote back only where
the row still names the same 3-credit course and may still vote. Anything else
is dropped and reported. The DB Admin delete, its previews, the Django admin
and the integrity check all say what happens to a vote.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from django.contrib import admin as django_admin
from django.contrib.admin.models import DELETION, LogEntry
from django.contrib.auth.models import Group, User
from django.contrib.messages import get_messages
from django.contrib.messages.storage.fallback import FallbackStorage
from django.db import IntegrityError
from django.test import Client, RequestFactory

from core.admin import ProgrammeRequirementAdmin
from core.models import AuditLog, ProgrammeRequirement
from core.services import db_admin_ops
from core.services.course_identity import planner_course_key
from core.services.db_admin_ops import (
    delete_program_catalog,
    import_oracle_plan_from_rows,
    import_program_plan,
    legacy_load_department_files_exact,
    preview_delete_program_catalog,
    preview_oracle_plan,
    run_integrity_checks,
)
from core.services.programme_meeting_patterns import budget_value, resolve_for_keys
from core.services.rbac import ROLE_SUPER_ADMIN, ensure_role_groups

pytestmark = pytest.mark.django_db

STAT = "PRINCIPLES OF STATISTICS"
ALGEBRA = "LINEAR ALGEBRA I"


@pytest.fixture(autouse=True)
def _no_file_backups(monkeypatch):
    """The test database is not a file; the importers' snapshot is not under test."""
    monkeypatch.setattr(
        db_admin_ops,
        "create_backup_snapshot",
        lambda: {"ok": True, "backup_file": "test.sqlite3", "size_bytes": 1},
    )


def plan_row(program, code, name, *, credits=3, vote="", kind="Mandatory", term=1):
    return ProgrammeRequirement.objects.create(
        program=program,
        course_code=code,
        course_name=name,
        type=kind,
        programme_term=term,
        credit_hours=credits,
        meeting_pattern=vote,
    )


def stored(program, code):
    return ProgrammeRequirement.objects.get(program=program, course_code=code)


def oracle_row(code, name, credits=3, kind="Mandatory", level=1):
    return {
        "code": code,
        "en_name": name,
        "credits": str(credits),
        "level_number": str(level),
        "type": kind,
        "is_online": 0,
        "prereqs_str": "",
    }


def csv_text(*rows):
    lines = ["course_code,programme_term,credit_hours,type"]
    lines += [f"{code},{term},{credits},Mandatory" for code, term, credits in rows]
    return "\n".join(lines) + "\n"


def super_admin_client() -> Client:
    ensure_role_groups()
    user, _ = User.objects.get_or_create(username="vote-import-admin")
    user.groups.clear()
    user.groups.add(Group.objects.get(name=ROLE_SUPER_ADMIN))
    client = Client()
    client.force_login(user)
    return client


def audit_details(action):
    row = AuditLog.objects.filter(action=action).order_by("-id").first()
    assert row is not None, f"no {action} audit row"
    return row, json.loads(row.details_json)


# ---------------------------------------------------------------------------
# The CSV importer
# ---------------------------------------------------------------------------


def test_a_csv_replace_keeps_each_course_name_and_its_vote():
    """The CSV has no name column; a replace used to wipe every name, so the
    row stopped naming the course its vote was for. The name now carries
    forward, the vote with it, and the course's budget still takes 2 x 100
    by the rule Generate applies (no Generate run needed)."""
    plan_row("AI2", "STAT103", STAT, vote="2x100")
    plan_row("AI2", "MATH243", ALGEBRA, vote="standard", term=3)
    plan_row("AI2", "CS111", "FUNDAMENTALS OF PROGRAMMING")

    result = import_program_plan(
        "AI2",
        csv_text(("STAT103", 1, 3), ("MATH243", 3, 3), ("CS111", 1, 3)),
        replace_existing=True,
    )

    assert [
        (r.course_code, r.course_name, r.meeting_pattern)
        for r in ProgrammeRequirement.objects.filter(program="AI2").order_by("course_code")
    ] == [
        ("CS111", "FUNDAMENTALS OF PROGRAMMING", ""),
        ("MATH243", ALGEBRA, "standard"),
        ("STAT103", STAT, "2x100"),
    ]
    assert result["meeting_patterns"] == {
        "kept": [
            {
                "program": "AI2",
                "course_code": "MATH243",
                "course_key": "MATH243::LINEAR_ALGEBRA_I",
                "meeting_pattern": "standard",
            },
            {
                "program": "AI2",
                "course_code": "STAT103",
                "course_key": "STAT103::PRINCIPLES_OF_STATISTICS",
                "meeting_pattern": "2x100",
            },
        ],
        "dropped": [],
    }
    key = planner_course_key("STAT103", STAT)
    decision = resolve_for_keys([key], ["AI2"])[key]
    assert (decision.status, decision.meeting_pattern) == ("agree", "2x100")
    assert budget_value(decision, course_key=key, course_code="STAT103", credit_hours=3) == "2x100"


def test_a_csv_upsert_that_moves_a_voted_course_to_four_credits_drops_the_vote():
    """The vote is cleared before the import writes, so the CHECK never sees
    2x100 on a 4-credit row: the import succeeds and says what it dropped."""
    plan_row("AI2", "STAT103", STAT, vote="2x100")

    result = import_program_plan("AI2", csv_text(("STAT103", 1, 4)))

    row = stored("AI2", "STAT103")
    assert (row.credit_hours, row.meeting_pattern, row.course_name) == (4, "", STAT)
    assert result["meeting_patterns"] == {
        "kept": [],
        "dropped": [
            {
                "program": "AI2",
                "course_code": "STAT103",
                "course_key": "STAT103::PRINCIPLES_OF_STATISTICS",
                "old": "2x100",
                "reason": "credits_changed",
            },
        ],
    }


def test_a_code_named_twice_in_one_file_ends_where_its_last_row_says():
    plan_row("AI2", "STAT103", STAT, vote="2x100")

    result = import_program_plan("AI2", csv_text(("STAT103", 1, 4), ("STAT103", 1, 3)))

    row = stored("AI2", "STAT103")
    assert (row.credit_hours, row.meeting_pattern) == (3, "2x100")
    assert [k["course_code"] for k in result["meeting_patterns"]["kept"]] == ["STAT103"]
    assert result["meeting_patterns"]["dropped"] == []


def test_an_upsert_leaves_the_votes_of_courses_outside_the_file_alone():
    plan_row("AI2", "STAT103", STAT, vote="2x100")
    plan_row("AI2", "MATH243", ALGEBRA, vote="2x100", term=3)

    result = import_program_plan("AI2", csv_text(("STAT103", 2, 3)))

    assert stored("AI2", "MATH243").meeting_pattern == "2x100"
    assert stored("AI2", "STAT103").programme_term == 2
    assert [k["course_code"] for k in result["meeting_patterns"]["kept"]] == ["STAT103"]


def test_an_import_that_touches_no_vote_answers_exactly_as_before():
    plan_row("AI2", "STAT103", STAT)

    result = import_program_plan("AI2", csv_text(("STAT103", 1, 4)), replace_existing=True)

    assert set(result) == {
        "ok",
        "program",
        "rows_processed",
        "rows_upserted",
        "replace_existing",
        "backup",
    }


# ---------------------------------------------------------------------------
# The Oracle importer
# ---------------------------------------------------------------------------


def test_an_oracle_rename_drops_the_vote_and_names_both_courses():
    """CS112 PROGRAMMING II and PROGRAMMING I are two courses; a vote chosen
    for one never moves to the other with the code."""
    plan_row("CS2", "CS112", "PROGRAMMING II", vote="2x100", term=2)
    plan_row("CS2", "MATH243", ALGEBRA, vote="2x100", term=3)  # not in the file

    result = import_oracle_plan_from_rows("CS2", [oracle_row("CS112", "PROGRAMMING I", level=2)])

    row = stored("CS2", "CS112")
    assert (row.course_name, row.meeting_pattern) == ("PROGRAMMING I", "")
    assert stored("CS2", "MATH243").meeting_pattern == "2x100"
    assert result["meeting_patterns"] == {
        "kept": [],
        "dropped": [
            {
                "program": "CS2",
                "course_code": "CS112",
                "course_key": "CS112::PROGRAMMING_II",
                "old": "2x100",
                "reason": "identity_changed",
                "new_course_key": "CS112::PROGRAMMING_I",
            },
        ],
    }


def test_an_oracle_edit_of_case_and_spacing_keeps_the_vote():
    plan_row("CS2", "CS112", "Programming  II", vote="2x100", term=2)

    result = import_oracle_plan_from_rows("CS2", [oracle_row("CS112", "PROGRAMMING II", level=2)])

    row = stored("CS2", "CS112")
    assert (row.course_name, row.meeting_pattern) == ("PROGRAMMING II", "2x100")
    assert result["meeting_patterns"]["dropped"] == []


def test_an_oracle_import_that_makes_the_course_an_elective_slot_drops_the_vote():
    plan_row("AI2", "STAT103", STAT, vote="standard")

    result = import_oracle_plan_from_rows(
        "AI2", [oracle_row("STAT103", STAT, kind="Program Elective")]
    )

    assert stored("AI2", "STAT103").meeting_pattern == ""
    assert [(d["course_code"], d["reason"]) for d in result["meeting_patterns"]["dropped"]] == [
        ("STAT103", "not_eligible")
    ]


def test_a_dropped_vote_reports_the_first_reason_that_applies():
    """Precedence: row_gone, credits_changed, identity_changed, not_eligible."""
    plan_row("CS2", "CS112", "PROGRAMMING II", vote="2x100", term=2)
    plan_row("CS2", "STAT103", STAT, vote="standard")

    result = import_oracle_plan_from_rows(
        "CS2",
        [
            oracle_row("CS112", "PROGRAMMING I", credits=4, level=2),
            oracle_row("STAT103", "PROGRAM ELECTIVE 1", kind="Program Elective"),
        ],
    )

    assert [(d["course_code"], d["reason"]) for d in result["meeting_patterns"]["dropped"]] == [
        ("CS112", "credits_changed"),
        ("STAT103", "identity_changed"),
    ]


def test_an_oracle_replace_drops_the_vote_of_a_course_the_file_leaves_out():
    plan_row("AI2", "STAT103", STAT, vote="2x100")
    plan_row("AI2", "MATH243", ALGEBRA, vote="2x100", term=3)

    result = import_oracle_plan_from_rows(
        "AI2", [oracle_row("MATH243", ALGEBRA, level=3)], replace_existing=True
    )

    assert stored("AI2", "MATH243").meeting_pattern == "2x100"
    assert not ProgrammeRequirement.objects.filter(program="AI2", course_code="STAT103").exists()
    assert [(d["course_code"], d["reason"]) for d in result["meeting_patterns"]["dropped"]] == [
        ("STAT103", "row_gone")
    ]
    assert [k["course_code"] for k in result["meeting_patterns"]["kept"]] == ["MATH243"]


# ---------------------------------------------------------------------------
# The legacy loader: one file, several programmes
# ---------------------------------------------------------------------------


def test_the_legacy_loader_reports_votes_per_programme(tmp_path: Path):
    plan_row("AI", "STAT103", STAT, vote="2x100")
    plan_row("AI", "MATH243", ALGEBRA, vote="2x100", term=3)  # not in the file
    plan_row("DS2", "MATH243", ALGEBRA, vote="standard", term=3)
    requirements = tmp_path / "requirements.csv"
    requirements.write_text(
        "program,course_code,type,programme_term,credit_hours\n"
        "AI,STAT103,Mandatory,2,3\n"
        "DS2,MATH243,Mandatory,3,4\n",
        encoding="utf-8",
    )
    prerequisites = tmp_path / "prerequisites.csv"
    prerequisites.write_text("program,course_code,prerequisite_course_code\n", encoding="utf-8")

    result = legacy_load_department_files_exact(str(requirements), str(prerequisites))

    assert result["meeting_patterns"] == {
        "AI": {
            "kept": [
                {
                    "program": "AI",
                    "course_code": "STAT103",
                    "course_key": "STAT103::PRINCIPLES_OF_STATISTICS",
                    "meeting_pattern": "2x100",
                },
            ],
            "dropped": [],
        },
        "DS2": {
            "kept": [],
            "dropped": [
                {
                    "program": "DS2",
                    "course_code": "MATH243",
                    "course_key": "MATH243::LINEAR_ALGEBRA_I",
                    "old": "standard",
                    "reason": "credits_changed",
                },
            ],
        },
    }
    assert stored("AI", "STAT103").meeting_pattern == "2x100"
    assert stored("AI", "MATH243").meeting_pattern == "2x100"
    assert (stored("DS2", "MATH243").credit_hours, stored("DS2", "MATH243").meeting_pattern) == (
        4,
        "",
    )


# ---------------------------------------------------------------------------
# The DB Admin views: audits and refusals
# ---------------------------------------------------------------------------


def test_an_import_audit_carries_the_votes_it_kept_and_dropped():
    plan_row("AI2", "STAT103", STAT, vote="2x100")
    plan_row("AI2", "MATH243", ALGEBRA, vote="2x100", term=3)
    client = super_admin_client()

    response = client.post(
        "/ops/db/import-program-plan/",
        data=json.dumps(
            {"program": "AI2", "csv_text": csv_text(("STAT103", 1, 3), ("MATH243", 3, 4))}
        ),
        content_type="application/json",
    )

    assert response.status_code == 200
    _, details = audit_details("db.import_program_plan")
    assert details["meeting_patterns"] == response.json()["meeting_patterns"]
    assert [k["course_code"] for k in details["meeting_patterns"]["kept"]] == ["STAT103"]
    assert [d["course_code"] for d in details["meeting_patterns"]["dropped"]] == ["MATH243"]


@pytest.mark.parametrize(
    ("url", "target", "body"),
    [
        (
            "/ops/db/import-program-plan/",
            "import_program_plan",
            {"program": "AI2", "csv_text": "course_code,programme_term,credit_hours\nX1,1,3\n"},
        ),
        ("/ops/db/import-legacy-exact/", "legacy_load_department_files_exact", {}),
        (
            "/ops/db/import-oracle-plan/",
            "import_oracle_plan_from_rows",
            {"program": "AI2", "rows": [oracle_row("X1", "X")]},
        ),
    ],
)
def test_an_import_the_database_refuses_is_a_400_with_an_error_audit(
    monkeypatch, url, target, body
):
    action = {
        "import_program_plan": "db.import_program_plan",
        "legacy_load_department_files_exact": "db.import_legacy_exact",
        "import_oracle_plan_from_rows": "db.import_oracle_plan",
    }[target]

    def refuse(*_args, **_kwargs):
        raise IntegrityError("CHECK constraint failed: ck_pr_meeting_pattern_fits_credits")

    monkeypatch.setattr(f"core.db_admin_views.{target}", refuse)
    client = super_admin_client()

    response = client.post(url, data=json.dumps(body), content_type="application/json")

    assert response.status_code == 400
    assert response.json()["error"].startswith("Nothing was imported")
    assert "ck_pr_meeting_pattern_fits_credits" in response.json()["error"]
    row, _ = audit_details(action)
    assert row.status == "error"
    assert "ck_pr_meeting_pattern_fits_credits" in row.error_text


def test_a_refused_import_answers_in_arabic_on_an_arabic_page(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise IntegrityError("CHECK constraint failed: ck_pr_meeting_pattern_fits_credits")

    monkeypatch.setattr("core.db_admin_views.import_oracle_plan_from_rows", refuse)
    client = super_admin_client()

    response = client.post(
        "/ops/db/import-oracle-plan/",
        data=json.dumps({"program": "AI2", "rows": [oracle_row("X1", "X")]}),
        content_type="application/json",
        HTTP_ACCEPT_LANGUAGE="ar",
    )

    assert response.status_code == 400
    assert response.json()["error"].startswith("لم يُستورد شيء")


# ---------------------------------------------------------------------------
# Deleting a catalogue, and the previews that come before
# ---------------------------------------------------------------------------


def test_deleting_a_catalogue_lists_the_votes_it_deletes_and_audits_them():
    plan_row("AI2", "STAT103", STAT, vote="2x100")
    plan_row("AI2", "MATH243", ALGEBRA, vote="standard", term=3)
    plan_row("AI2", "CS111", "FUNDAMENTALS OF PROGRAMMING")
    plan_row("AI", "STAT103", STAT, vote="2x100")

    assert preview_delete_program_catalog("AI2")["meeting_patterns_count"] == 2
    client = super_admin_client()
    response = client.post(
        "/ops/db/delete-program-catalog/",
        data=json.dumps({"program": "AI2", "confirm": "DELETE"}),
        content_type="application/json",
    )

    assert response.status_code == 200
    listed = [
        {
            "course_code": "MATH243",
            "course_key": "MATH243::LINEAR_ALGEBRA_I",
            "meeting_pattern": "standard",
        },
        {
            "course_code": "STAT103",
            "course_key": "STAT103::PRINCIPLES_OF_STATISTICS",
            "meeting_pattern": "2x100",
        },
    ]
    assert response.json()["deleted_meeting_patterns"] == listed
    _, details = audit_details("db.delete_program_catalog")
    assert details["deleted_meeting_patterns"] == listed
    assert stored("AI", "STAT103").meeting_pattern == "2x100"


def test_a_catalogue_without_votes_previews_and_deletes_exactly_as_before():
    plan_row("AI2", "CS111", "FUNDAMENTALS OF PROGRAMMING")

    assert set(preview_delete_program_catalog("AI2")) == {
        "program",
        "requirements_count",
        "prerequisites_count",
    }
    assert "deleted_meeting_patterns" not in delete_program_catalog("AI2")


def test_the_oracle_preview_counts_the_votes_an_import_may_drop(monkeypatch):
    from core.services import oracle_plan_parser

    monkeypatch.setattr(
        oracle_plan_parser,
        "parse_oracle_plan",
        lambda *_a, **_k: {"levels": {}, "metadata": {}, "summary": {}, "warnings": []},
    )
    plan_row("AI2", "STAT103", STAT, vote="2x100")
    plan_row("AI2", "CS111", "FUNDAMENTALS OF PROGRAMMING")

    assert preview_oracle_plan("AI2", content="x")["existing_db"] == {
        "requirements": 2,
        "prerequisites": 0,
        "meeting_patterns": 1,
    }
    assert preview_oracle_plan("CS2", content="x")["existing_db"] == {
        "requirements": 0,
        "prerequisites": 0,
    }


# ---------------------------------------------------------------------------
# The Django admin
# ---------------------------------------------------------------------------


@pytest.fixture
def admin_client(client):
    user = User.objects.create_superuser("vote-admin", "vote-admin@example.invalid", "x")
    client.force_login(user)
    return client


def change_form(row, **changes):
    data = {
        "program": row.program,
        "course_code": row.course_code,
        "course_name": row.course_name,
        "type": row.type,
        "programme_term": str(row.programme_term),
        "credit_hours": str(row.credit_hours),
        "max_capacity": "",
        "_save": "Save",
    }
    data.update(changes)
    return data


def test_the_admin_will_not_move_a_voted_course_off_three_credits_or_rename_it(admin_client):
    row = plan_row("AI2", "STAT103", STAT, vote="2x100")
    url = f"/admin/core/programmerequirement/{row.pk}/change/"

    page = admin_client.get(url)
    assert page.status_code == 200
    assert "clear its weekly meetings in Section Planning first" in page.content.decode()
    assert f"/admin/core/programmerequirement/{row.pk}/delete/" not in page.content.decode()

    response = admin_client.post(
        url, change_form(row, credit_hours="4", course_name="RENAMED", programme_term="2")
    )

    assert response.status_code == 302
    row.refresh_from_db()
    assert (row.credit_hours, row.course_name, row.meeting_pattern) == (3, STAT, "2x100")
    assert row.programme_term == 2  # the fields a vote does not depend on stay editable
    assert admin_client.get(f"/admin/core/programmerequirement/{row.pk}/delete/").status_code == 403


def test_the_admin_still_edits_a_row_without_a_vote_and_never_its_vote(admin_client):
    row = plan_row("AI2", "CS111", "FUNDAMENTALS OF PROGRAMMING")
    url = f"/admin/core/programmerequirement/{row.pk}/change/"

    response = admin_client.post(url, change_form(row, credit_hours="4", meeting_pattern="2x100"))

    assert response.status_code == 302
    row.refresh_from_db()
    assert (row.credit_hours, row.meeting_pattern) == (4, "")


def test_the_admin_bulk_delete_keeps_voted_rows_and_counts_only_what_it_deletes(admin_client):
    voted = plan_row("AI2", "STAT103", STAT, vote="2x100")
    plain = plan_row("AI2", "CS111", "FUNDAMENTALS OF PROGRAMMING")

    response = admin_client.post(
        "/admin/core/programmerequirement/",
        {"action": "delete_selected", "_selected_action": [voted.pk, plain.pk], "post": "yes"},
    )

    assert response.status_code == 302
    assert ProgrammeRequirement.objects.filter(pk=voted.pk, meeting_pattern="2x100").exists()
    assert not ProgrammeRequirement.objects.filter(pk=plain.pk).exists()
    said = [str(m) for m in get_messages(response.wsgi_request)]
    assert any("AI2 STAT103" in m and "Section Planning" in m for m in said), said
    assert any("Successfully deleted 1 " in m for m in said), said
    deleted = LogEntry.objects.filter(action_flag=DELETION).values_list("object_id", flat=True)
    assert set(deleted) == {str(plain.pk)}


def test_the_admin_delete_queryset_itself_never_deletes_a_voted_row():
    voted = plan_row("AI2", "STAT103", STAT, vote="standard")
    plain = plan_row("AI2", "CS111", "FUNDAMENTALS OF PROGRAMMING")
    request = RequestFactory().post("/admin/core/programmerequirement/")
    request.session = {}  # type: ignore[attr-defined]
    request._messages = FallbackStorage(request)  # type: ignore[attr-defined]
    model_admin = ProgrammeRequirementAdmin(ProgrammeRequirement, django_admin.site)

    model_admin.delete_queryset(
        request, ProgrammeRequirement.objects.filter(pk__in=[voted.pk, plain.pk])
    )

    assert ProgrammeRequirement.objects.filter(pk=voted.pk).exists()
    assert not ProgrammeRequirement.objects.filter(pk=plain.pk).exists()
    assert any("AI2 STAT103" in str(m) for m in request._messages)


# ---------------------------------------------------------------------------
# The integrity check
# ---------------------------------------------------------------------------


def test_the_integrity_check_lists_votes_on_rows_that_may_no_longer_vote():
    plan_row("AI2", "STAT103", STAT, vote="2x100")  # may vote: not listed
    slot = plan_row("AI2", "AI1", "PROGRAM ELECTIVE 1", vote="standard", kind="Program Elective")
    nameless = plan_row("CS2", "MATH243", "", vote="2x100")

    result = run_integrity_checks()

    assert result["meeting_patterns_not_eligible"] == 2
    assert result["meeting_patterns_not_eligible_details"] == [
        f"AI2/AI1 PROGRAM ELECTIVE 1: standard [PLACEHOLDER] (programme_requirements.id={slot.pk})",
        f"CS2/MATH243 (no course name): 2x100 [NOT_NAMED] "
        f"(programme_requirements.id={nameless.pk})",
    ]
    assert "Section Planning" in result["advice"]["meeting_patterns_not_eligible"]


def test_the_integrity_check_is_quiet_when_every_vote_may_stand():
    plan_row("AI2", "STAT103", STAT, vote="2x100")

    result = run_integrity_checks()

    assert result["meeting_patterns_not_eligible"] == 0
    assert result["meeting_patterns_not_eligible_details"] == []


@pytest.mark.parametrize("language", ["en", "ar"])
def test_the_integrity_report_counts_stranded_votes_in_its_audit(language):
    plan_row("AI2", "AI1", "PROGRAM ELECTIVE 1", vote="standard", kind="Program Elective")
    client = super_admin_client()

    response = client.get("/ops/db/integrity-report/", HTTP_ACCEPT_LANGUAGE=language)

    assert response.status_code == 200
    advice = response.json()["advice"]["meeting_patterns_not_eligible"]
    assert ("تخطيط الشعب" in advice) == (language == "ar")
    assert ("Section Planning" in advice) == (language == "en")
    _, details = audit_details("db.integrity_report")
    assert details["meeting_patterns_not_eligible"] == 1


# ---------------------------------------------------------------------------
# The DB Admin page: its confirmations say what happens to the votes
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("language", ["en", "ar"])
def test_the_db_admin_confirmations_say_what_happens_to_weekly_meetings(
    tmp_path: Path, language: str
):
    """Runs tests/frontend/db-admin-meeting-patterns.test.cjs (jsdom) on the
    page the real view renders; needs Node.js and the locked npm dependencies."""
    node = shutil.which("node")
    if not node or not (ROOT / "node_modules/jsdom/package.json").is_file():
        pytest.skip("DB Admin DOM tests require Node.js and the locked npm ci dependencies")
    client = super_admin_client()
    response = client.get("/db-admin/", HTTP_ACCEPT_LANGUAGE=language)
    assert response.status_code == 200
    html = response.content.decode("utf-8")
    assert f'lang="{language}"' in html
    fixture = tmp_path / f"db-admin-{language}.html"
    fixture.write_text(html, encoding="utf-8")

    result = subprocess.run(
        [node, "--test", "tests/frontend/db-admin-meeting-patterns.test.cjs"],
        cwd=ROOT,
        env={**os.environ, "DBA_TEST_HTML": str(fixture), "DBA_TEST_LANGUAGE": language},
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
