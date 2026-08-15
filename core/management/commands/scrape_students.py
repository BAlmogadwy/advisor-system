"""Batch-scrape student study plans and timetables from the university portal.

Usage:
    python manage.py scrape_students --csv data/students_list.csv
    python manage.py scrape_students --database-students
    python manage.py scrape_students --csv data/students_list.csv --concurrency 4 --save-html
    python manage.py scrape_students --csv data/students_list.csv \
        --empty-snapshot-year 1448 --empty-snapshot-term 1
"""

from __future__ import annotations

import asyncio
import csv
import logging
import math
import os
import random
import re
import threading
from argparse import ArgumentParser
from pathlib import Path
from typing import Any, TypedDict

from asgiref.sync import sync_to_async
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from tqdm import tqdm  # type: ignore[import-untyped]

from core.services.scrape_student_source import ScrapeStudentRow, load_database_students
from core.services.section_snapshot_guard import section_snapshot_operation_guard

try:
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError

    from core.services.portal_scraper import (
        close_browser,
        create_fresh_page_from_context,
        login_to_portal,
        navigate_to_student_study_plan,
        navigate_to_student_timetable,
        safe_page_content,
    )

    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False

logger = logging.getLogger(__name__)
BASE_DIR = Path(settings.BASE_DIR)
MIN_PROGRAMME_PLAN_OVERLAP = 0.80


class StudentScrapeOutcome(TypedDict):
    student_id: str
    program: str
    ok: bool
    schedule_state: str
    academic_year: str
    term: str
    error: str


class PortalSessionRecoveryError(RuntimeError):
    """A shared portal session could not be restored for the batch."""


# ------------------------------------------------------------------
# Utilities
# ------------------------------------------------------------------


def _sid_to_str(student_id: object) -> str:
    try:
        if isinstance(student_id, float) and student_id.is_integer():
            return str(int(student_id))
    except Exception:
        logger.debug("student_id conversion failed for %r", student_id, exc_info=True)
    return str(student_id).strip()


def _elective_scope_for_program(
    outcomes: list[StudentScrapeOutcome],
    program: str,
) -> tuple[list[int], dict[int, tuple[str, str]]]:
    """Build post-processing scope only from attributed successful workers."""
    successful = [
        outcome for outcome in outcomes if outcome["ok"] and outcome["program"] == program
    ]
    student_ids = sorted({int(outcome["student_id"]) for outcome in successful})
    snapshots = {
        int(outcome["student_id"]): (outcome["academic_year"], outcome["term"])
        for outcome in successful
        if outcome["schedule_state"] == "complete_schedule"
        and outcome["academic_year"]
        and outcome["term"]
    }
    return student_ids, snapshots


def _empty_snapshot_scope(options: dict[str, Any]) -> tuple[str, str]:
    """Validate the optional operator-supplied term for empty timetables.

    A confirmed-empty portal response contains no term metadata.  The scraper
    can therefore clear an expected plan for that term only when the operator
    supplies both parts of the target explicitly.
    """
    academic_year = str(options.get("empty_snapshot_year") or "").strip()
    term = str(options.get("empty_snapshot_term") or "").strip()
    if bool(academic_year) != bool(term):
        raise CommandError(
            "--empty-snapshot-year and --empty-snapshot-term must be supplied together."
        )
    if academic_year and not re.fullmatch(r"[0-9]{4}", academic_year):
        raise CommandError("--empty-snapshot-year must be a four-digit academic year.")
    if term and term not in {"1", "2", "3"}:
        raise CommandError("--empty-snapshot-term must be 1, 2, or 3.")
    return academic_year, term


def _validate_study_plan(study_data: list[dict]) -> tuple[bool, str]:
    if not study_data:
        return False, "Study plan parsed empty"
    if len(study_data) < 25:
        return False, "Too few courses parsed"
    terms = [c.get("programme_term") for c in study_data if c.get("programme_term") is not None]
    if len(set(terms)) < 3:
        return False, "Too few distinct terms parsed"
    for course in study_data:
        if not course.get("dept") or not course.get("no") or not course.get("description"):
            return False, "Study plan contains an incomplete course row"
        credits = course.get("ue")
        if isinstance(credits, bool) or not isinstance(credits, int) or credits <= 0:
            return False, "Study plan contains invalid course credits"
        programme_term = course.get("programme_term")
        if (
            isinstance(programme_term, bool)
            or not isinstance(programme_term, int)
            or programme_term <= 0
        ):
            return False, "Study plan contains an invalid programme term"
    return True, "OK"


def _require_nonempty_text(data: dict[str, Any], key: str, label: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} could not be parsed")
    return value.strip()


def _require_nonnegative_int(data: dict[str, Any], key: str, label: str) -> int:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def _validate_student_profile(profile: dict[str, Any], *, expected_student_id: str) -> None:
    """Require every profile field used to overwrite the stored student row."""
    registration_no = _require_nonempty_text(
        profile,
        "registration_no",
        "Study-plan registration number",
    )
    if registration_no != str(expected_student_id).strip():
        raise ValueError(
            f"Study plan belongs to student {registration_no}, not requested student "
            f"{expected_student_id}"
        )
    _require_nonempty_text(profile, "name", "Student name")
    _require_nonempty_text(profile, "nationality", "Student nationality")
    _require_nonempty_text(profile, "status", "Student status")
    _require_nonnegative_int(
        profile,
        "total_registered_credits",
        "Total registered credits",
    )
    _require_nonnegative_int(
        profile,
        "total_earned_credits",
        "Total earned credits",
    )

    if "gpa" not in profile:
        raise ValueError("GPA field is missing from study-plan profile")
    gpa = profile["gpa"]
    if gpa is None:
        return
    if (
        isinstance(gpa, bool)
        or not isinstance(gpa, int | float)
        or not math.isfinite(float(gpa))
        or not 0 <= float(gpa) <= 5
    ):
        raise ValueError("GPA must be a number between 0 and 5")


def _validate_study_plan_for_program(study_data: list[dict], program: str) -> None:
    """Reject an otherwise valid plan that belongs to a different programme."""
    from core.models import ProgrammeRequirement
    from core.services.student_helpers import normalize_code

    normalized_program = str(program or "").strip().upper()
    expected_codes = {
        normalize_code(code)
        for code in ProgrammeRequirement.objects.filter(program=normalized_program).values_list(
            "course_code", flat=True
        )
    }
    # Some deployments scrape a programme before importing its requirement
    # catalogue. Keep the existing workflow available in that case; when a
    # catalogue exists, it is the authoritative programme identity check.
    if not expected_codes:
        return

    parsed_codes = {normalize_code(f"{course['dept']}{course['no']}") for course in study_data}
    matching = parsed_codes & expected_codes
    overlap = len(matching) / len(expected_codes)
    if overlap < MIN_PROGRAMME_PLAN_OVERLAP:
        raise ValueError(
            f"Study plan does not match CSV programme {normalized_program}: "
            f"only {len(matching)}/{len(expected_codes)} configured requirements matched"
        )


def _parse_and_validate_student_response(
    student_id: str,
    study_html: str,
    timetable_html: str,
    program: str,
) -> dict[str, Any]:
    """Parse every required portal component before the first ORM operation."""
    from core.services.course_classifier import classify_courses
    from core.services.student_helpers import normalize_code
    from core.services.student_parser import (
        parse_student_profile,
        parse_study_plan,
        parse_timetable_info,
    )
    from core.services.student_timetable_ingest import validate_timetable_response

    study_data = parse_study_plan(study_html)
    ok, message = _validate_study_plan(study_data)
    if not ok:
        raise ValueError(message)
    _validate_study_plan_for_program(study_data, program)

    profile = parse_student_profile(study_html)
    _validate_student_profile(profile, expected_student_id=student_id)

    timetable_info = parse_timetable_info(timetable_html)
    validated_timetable = validate_timetable_response(
        timetable_html,
        expected_student_id=student_id,
        expected_registered_credits=timetable_info.get("current_registered_credits"),
        # The study-plan identity was verified immediately above. Chrome and
        # captured production HTML confirm that the timetable service returns
        # its misleading "student number is wrong" page for a real student who
        # has no current summer timetable.
        allow_confirmed_empty=True,
    )
    current_registered_credits = validated_timetable["current_registered_credits"]
    timetable_info = {
        **timetable_info,
        "current_registered_credits": current_registered_credits,
    }
    structured_course_codes = {
        normalize_code(f"{row['course_code']}{row['course_number']}")
        for row in validated_timetable["rows"]
    }
    if (
        not structured_course_codes
        and validated_timetable["schedule_state"] != "confirmed_empty_current_schedule"
    ):
        raise ValueError("Timetable course list parsed empty")

    return {
        "study_data": study_data,
        "profile": profile,
        "timetable_info": timetable_info,
        "classification": classify_courses(study_data, structured_course_codes),
        "current_course_codes": structured_course_codes,
        "validated_timetable": validated_timetable,
    }


# ------------------------------------------------------------------
# Sync student processing (Django ORM — runs in a thread)
# ------------------------------------------------------------------

_db_lock = threading.Lock()


def _process_student(
    student_id: str,
    study_html: str,
    timetable_html: str,
    program: str,
    section: str,
    *,
    empty_snapshot_year: str = "",
    empty_snapshot_term: str = "",
) -> dict:
    """Parse scraped HTML and persist via Django ORM.  Pure sync."""
    from django.db import transaction

    from core.models import Course, Student, StudentCourse
    from core.services.course_classifier import parse_course_result
    from core.services.student_helpers import normalize_code
    from core.services.student_sections import clear_student_section_snapshot
    from core.services.student_timetable_ingest import ingest_student_timetable_html

    # ── Parse ──────────────────────────────────────────────────
    parsed = _parse_and_validate_student_response(
        student_id,
        study_html,
        timetable_html,
        program,
    )
    study_data = parsed["study_data"]
    classification = parsed["classification"]
    profile = parsed["profile"]
    tt_info = parsed["timetable_info"]
    current_course_codes = parsed["current_course_codes"]
    validated_timetable = parsed["validated_timetable"]

    # ── Prepare data before acquiring lock ───────────────────
    sid = int(student_id)
    study_plan_codes = {normalize_code(f"{c['dept']} {c['no']}") for c in study_data}

    # ── Serialize all DB writes (SQLite single-writer) ─────
    with _db_lock, transaction.atomic():
        defaults = {
            "registration_no": student_id,
            "name": profile.get("name", "Unknown"),
            "nationality": profile.get("nationality", "Unknown"),
            "status": profile.get("status", "Active"),
            "gpa": float(profile["gpa"]) if isinstance(profile["gpa"], int | float) else None,
            "total_registered_credits": profile.get("total_registered_credits")
            if isinstance(profile.get("total_registered_credits"), int)
            else 0,
            "total_earned_credits": profile.get("total_earned_credits")
            if isinstance(profile.get("total_earned_credits"), int)
            else 0,
            "current_registered_credits": tt_info.get("current_registered_credits")
            if isinstance(tt_info.get("current_registered_credits"), int)
            else 0,
            "program": program,
            "section": section,
        }

        # Only set advisor_id if the student is new or has no advisor yet.
        # This preserves integer IDs assigned by seed_advisors on re-scrape.
        existing = (
            Student.objects.filter(student_id=sid).values_list("advisor_id", flat=True).first()
        )
        if not existing:
            defaults["advisor_id"] = tt_info.get("advisor_name", "")

        Student.objects.update_or_create(
            student_id=sid,
            defaults=defaults,
        )

        if validated_timetable["schedule_state"] == "confirmed_empty_current_schedule":
            clear_result = clear_student_section_snapshot(
                sid,
                academic_year=empty_snapshot_year,
                term=empty_snapshot_term,
            )
            bridge_res = {
                "ok": True,
                "schedule_state": "confirmed_empty_current_schedule",
                "academic_year": empty_snapshot_year,
                "term": empty_snapshot_term,
                "parsed_rows": 0,
                "mapped_sections": 0,
                "missing_links": 0,
                "external_courses_created": 0,
                "external_courses": [],
                "deleted_student_section_links": clear_result["deleted"],
            }
        else:
            bridge_res = ingest_student_timetable_html(
                student_id=student_id,
                timetable_html=timetable_html,
                study_plan_codes=study_plan_codes,
                validated_response=validated_timetable,
            )
        existing_result_by_code = {
            normalize_code(code): {
                "status": status,
                "letter": grade,
                "marks": mark,
            }
            for code, status, grade, mark in StudentCourse.objects.filter(
                student_id=sid,
                course__course_code__in=study_plan_codes,
            ).values_list("course__course_code", "status", "grade", "mark")
        }

        for course in study_data:
            code = normalize_code(f"{course['dept']} {course['no']}")
            credit_hours = int(course.get("ue", 0)) if str(course.get("ue", "")).isdigit() else 0

            course_obj, _ = Course.objects.get_or_create(
                course_code=code,
                defaults={
                    "department": course["dept"],
                    "description": course["description"],
                    "credit_hours": credit_hours,
                },
            )

            status = (
                "passed"
                if code in classification["passed"]
                else "studying"
                if code in classification["studying"]
                else "failed"
                if code in classification["failed"]
                else "not_taken"
            )

            course_result = parse_course_result(course)
            if not course_result["has_snapshot"] and code in existing_result_by_code:
                previous = existing_result_by_code[code]
                previous_outcome = (
                    previous["status"] if previous["status"] in {"passed", "failed"} else None
                )
                if previous_outcome is None:
                    # During a retake the row status is ``studying`` while its
                    # last settled F/DN/NF result remains stored. If the next
                    # portal response is blank and the course is no longer in
                    # the timetable, recover that outcome from the preserved
                    # atomic result pair instead of demoting it to not_taken.
                    try:
                        previous_outcome = parse_course_result(previous)["outcome"]
                    except ValueError:
                        # Legacy databases may contain free-text values from
                        # before result validation. They are not evidence of a
                        # settled outcome and must not block an otherwise valid
                        # scrape.
                        previous_outcome = None

                if previous_outcome == "passed":
                    status = "passed"
                elif code in current_course_codes:
                    status = "studying"
                elif previous_outcome == "failed":
                    status = "failed"
            course_defaults: dict[str, object] = {
                "programme_term": course.get("programme_term"),
                "status": status,
            }
            if course_result["has_snapshot"]:
                course_defaults["grade"] = course_result["grade"]
                course_defaults["mark"] = course_result["mark"]

            StudentCourse.objects.update_or_create(
                student_id=sid,
                course=course_obj,
                defaults=course_defaults,
            )

        # Reconcile every residual studying row that was not represented by the
        # verified current timetable. This includes external courses, legacy
        # rows misclassified as regular courses, and courses left behind after
        # a programme change. Preserve a settled result when one exists.
        stale_studying_rows = StudentCourse.objects.filter(
            student_id=sid,
            status="studying",
        ).exclude(course__course_code__in=current_course_codes)
        for stale in stale_studying_rows.select_related("course"):
            try:
                stale_outcome = parse_course_result({"letter": stale.grade, "marks": stale.mark})[
                    "outcome"
                ]
            except ValueError:
                stale_outcome = None
            stale.status = stale_outcome or "not_taken"
            stale.save(update_fields=["status"])

    return bridge_res


# ------------------------------------------------------------------
# Command
# ------------------------------------------------------------------


class Command(BaseCommand):
    help = "Scrape student study plans and timetables from the university portal"
    _shared: dict[str, Any]

    def add_arguments(self, parser: ArgumentParser) -> None:
        source = parser.add_mutually_exclusive_group(required=True)
        source.add_argument("--csv", help="Path to students_list.csv")
        source.add_argument(
            "--database-students",
            action="store_true",
            help="Scrape the reviewed current-student roster stored in the database",
        )
        parser.add_argument("--expected-database-student-count", type=int)
        parser.add_argument("--expected-database-roster-sha256", default="")
        parser.add_argument("--concurrency", type=int, choices=range(1, 9), default=4)
        parser.add_argument("--save-html", action="store_true")
        parser.add_argument("--max-retries", type=int, default=2)
        parser.add_argument("--debug-dir", default="data/debug_failures")
        parser.add_argument(
            "--empty-snapshot-year",
            default="",
            metavar="YYYY",
            help=(
                "Academic year whose expected-plan links should also be cleared "
                "after a verified empty timetable; requires --empty-snapshot-term"
            ),
        )
        parser.add_argument(
            "--empty-snapshot-term",
            default="",
            choices=("1", "2", "3"),
            help=(
                "Term whose expected-plan links should also be cleared after a "
                "verified empty timetable; requires --empty-snapshot-year"
            ),
        )

    def handle(self, *args: Any, **options: Any) -> None:
        concurrency = options.get("concurrency", 4)
        if isinstance(concurrency, bool) or not isinstance(concurrency, int):
            raise CommandError("--concurrency must be an integer between 1 and 8.")
        if concurrency < 1 or concurrency > 8:
            raise CommandError("--concurrency must be between 1 and 8.")
        options["concurrency"] = concurrency
        expected_count = options.get("expected_database_student_count")
        expected_sha256 = str(options.get("expected_database_roster_sha256") or "").strip()
        if not options.get("database_students") and (expected_count is not None or expected_sha256):
            raise CommandError("Database roster expectations require --database-students.")
        if expected_count is not None and expected_count < 1:
            raise CommandError("--expected-database-student-count must be positive.")
        if expected_sha256 and re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None:
            raise CommandError(
                "--expected-database-roster-sha256 must be a lowercase SHA-256 digest."
            )
        empty_snapshot_year, empty_snapshot_term = _empty_snapshot_scope(options)
        options["empty_snapshot_year"] = empty_snapshot_year
        options["empty_snapshot_term"] = empty_snapshot_term
        if not HAS_PLAYWRIGHT:
            raise CommandError(
                "playwright is not installed. Install requirements-dev.txt for scraping: "
                "pip install -r requirements-dev.txt"
            )

        # A scraper launched by ``start_batch_scrape`` begins while the launcher
        # still owns the non-blocking transition lock. Waiting here lets the
        # launcher publish the child PID before this process takes ownership for
        # the complete scrape. Direct command invocations use the same guard.
        with section_snapshot_operation_guard(blocking=True) as acquired:
            if not acquired:
                # Blocking acquisition should only fail on an unexpected OS/file
                # lock error. Refuse to write a partial section snapshot.
                raise CommandError(
                    "Could not acquire the section snapshot operation guard; scrape aborted."
                )
            asyncio.run(self._run(options))

    # ──────────────────────────────────────────────────────────
    # Async orchestrator
    # ──────────────────────────────────────────────────────────

    async def _run(self, options: dict[str, Any]) -> None:
        concurrency = options["concurrency"]
        max_retries = options["max_retries"]
        save_html = options["save_html"]
        debug_dir = options["debug_dir"]
        empty_snapshot_year = str(options.get("empty_snapshot_year") or "")
        empty_snapshot_term = str(options.get("empty_snapshot_term") or "")

        if options.get("database_students"):
            students = await sync_to_async(load_database_students, thread_sensitive=True)(
                expected_count=options.get("expected_database_student_count"),
                expected_roster_sha256=str(options.get("expected_database_roster_sha256") or ""),
            )
            source_label = "the reviewed current-student database roster"
        else:
            csv_path = str(options.get("csv") or "").strip()
            if not csv_path:
                raise CommandError(
                    "Choose exactly one student source: --csv or --database-students."
                )
            students = self._read_csv(csv_path)
            source_label = csv_path
        student_word = "student" if len(students) == 1 else "students"
        self.stdout.write(f"Loaded {len(students)} {student_word} from {source_label}")

        os.makedirs(debug_dir, exist_ok=True)
        if save_html:
            os.makedirs("data/raw_html", exist_ok=True)

        # Login
        playwright_obj, browser, page = await login_to_portal()
        self._shared = {
            "playwright": playwright_obj,
            "browser": browser,
            "context": page.context,
            "page": page,
            "session_generation": 0,
        }

        sem = asyncio.Semaphore(concurrency)
        plan_sem = asyncio.Semaphore(3)
        relogin_lock = asyncio.Lock()
        failed_ids: list[str] = []
        outcomes: list[StudentScrapeOutcome] = []
        infrastructure_errors: list[str] = []

        self.stdout.write(
            f"Starting scrape for {len(students)} students (concurrency={concurrency})"
        )

        # Dispatch workers
        tasks = [
            asyncio.create_task(
                self._scrape_one(
                    s,
                    sem,
                    plan_sem,
                    relogin_lock,
                    max_retries,
                    save_html,
                    debug_dir,
                    empty_snapshot_year=empty_snapshot_year,
                    empty_snapshot_term=empty_snapshot_term,
                )
            )
            for s in students
        ]

        for fut in tqdm(asyncio.as_completed(tasks), total=len(tasks), desc="Scraping students"):
            try:
                outcome = await fut
                outcomes.append(outcome)
                if not outcome["ok"]:
                    failed_ids.append(outcome["student_id"])
            except Exception as exc:
                logger.error("Task failed: %s", exc)
                infrastructure_errors.append(str(exc))
                # A worker exception that escapes attribution is a shared
                # infrastructure failure. Continuing could write a mixture of
                # pre- and post-recovery snapshots, so stop the batch once.
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                break

        # This file represents the latest run, not cumulative history. Always
        # rewrite it so a successful retry cannot leave stale failure IDs.
        fail_path = BASE_DIR / "data" / "failed_scrapes.csv"
        fail_path.parent.mkdir(exist_ok=True)
        with fail_path.open("w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["failed_student_id"])
            for sid in failed_ids:
                w.writerow([sid])
        if failed_ids:
            self.stdout.write(
                self.style.WARNING(f"{len(failed_ids)} failures saved to {fail_path}")
            )

        await close_browser(playwright_obj, browser)

        # An unassociated task failure means the batch cannot prove which CSV
        # student completed. Never run post-processing against possibly stale
        # rows or report a false success count in that state.
        if infrastructure_errors:
            raise CommandError(
                "Student scrape infrastructure failed; elective resolution was skipped. "
                f"First error: {infrastructure_errors[0]}"
            )

        # Post-scrape: resolve elective placeholders by cross-referencing
        # timetable courses against unfulfilled plan placeholders.
        # This updates StudentCourse status from "not_taken" to "studying"
        # for placeholder courses (IS1, FE2, GSE1, etc.) when the student's
        # timetable shows they're studying a course that fulfills it.
        self.stdout.write("Resolving elective placeholders...")
        from core.services.elective_resolver import resolve_elective_placeholders

        successful_outcomes = [outcome for outcome in outcomes if outcome["ok"]]
        programs_in_batch = {
            outcome["program"] for outcome in successful_outcomes if outcome["program"]
        }
        total_resolved = 0
        total_updates = 0
        for prog in sorted(programs_in_batch):
            if not prog:
                continue
            successful_student_ids, verified_snapshots = _elective_scope_for_program(
                successful_outcomes,
                prog,
            )
            if not successful_student_ids:
                continue
            # Sync ORM work — run in a thread, same as _process_student.
            # Calling it directly on the event-loop thread raises
            # SynchronousOnlyOperation.
            result = await asyncio.to_thread(
                resolve_elective_placeholders,
                prog,
                student_ids=successful_student_ids,
                student_snapshots=verified_snapshots,
            )
            total_resolved += result["resolved_count"]
            total_updates += result["total_updates"]
            if result["resolved_count"] > 0:
                self.stdout.write(
                    f"  {prog}: {result['resolved_count']}/{result['total_students']} "
                    f"students, {result['total_updates']} placeholders resolved"
                )
        self.stdout.write(
            self.style.SUCCESS(
                f"Elective resolver: {total_resolved} students, {total_updates} updates"
            )
        )

        self.stdout.write(
            self.style.SUCCESS(
                f"Done. {len(successful_outcomes)} succeeded, {len(failed_ids)} failed."
            )
        )
        if failed_ids:
            raise CommandError(
                f"Student scrape completed with {len(failed_ids)} failed student(s); "
                f"see {fail_path}."
            )

    # ──────────────────────────────────────────────────────────
    # Per-student async worker
    # ──────────────────────────────────────────────────────────

    async def _scrape_one(
        self,
        student: ScrapeStudentRow,
        sem: asyncio.Semaphore,
        plan_sem: asyncio.Semaphore,
        relogin_lock: asyncio.Lock,
        max_retries: int,
        save_html: bool,
        debug_dir: str,
        *,
        empty_snapshot_year: str = "",
        empty_snapshot_term: str = "",
    ) -> StudentScrapeOutcome:
        """Return an attributed success/failure outcome for one student."""
        async with sem:
            student_id = _sid_to_str(student["student_id"])
            program = student.get("program", "")
            section = student.get("section", "")
            worker_page = None
            study_html = ""
            timetable_html = ""
            session_generation = int(self._shared.get("session_generation", 0))

            try:
                worker_page = await create_fresh_page_from_context(
                    self._shared["context"],  # type: ignore[arg-type]
                    referer_url=self._worker_referer_url(),
                )
                for attempt in range(1, max_retries + 1):
                    try:
                        async with plan_sem:
                            study_html = await navigate_to_student_study_plan(
                                worker_page,
                                student_id,
                                verbose=False,
                            )

                        timetable_html = await navigate_to_student_timetable(
                            worker_page,
                            student_id,
                            verbose=False,
                        )

                        # Process in thread — pure sync Django ORM
                        bridge_res = await asyncio.to_thread(
                            _process_student,
                            student_id,
                            study_html,
                            timetable_html,
                            program,
                            section,
                            empty_snapshot_year=empty_snapshot_year,
                            empty_snapshot_term=empty_snapshot_term,
                        )
                        logger.info("[TT-LINK] %s: %s", student_id, bridge_res)

                        if save_html:
                            html_dir = BASE_DIR / "data" / "raw_html"
                            (html_dir / f"{student_id}_study.html").write_text(
                                study_html,
                                encoding="utf-8",
                            )
                            (html_dir / f"{student_id}_timetable.html").write_text(
                                timetable_html,
                                encoding="utf-8",
                            )

                        return {
                            "student_id": student_id,
                            "program": str(program or ""),
                            "ok": True,
                            "schedule_state": str(bridge_res.get("schedule_state") or ""),
                            "academic_year": str(bridge_res.get("academic_year") or ""),
                            "term": str(bridge_res.get("term") or ""),
                            "error": "",
                        }

                    except RuntimeError as exc:
                        if "SESSION_LOGGED_OUT_HTML" in str(exc):
                            session_generation = await self._force_relogin(
                                relogin_lock,
                                observed_generation=session_generation,
                            )
                            if worker_page is not None:
                                try:
                                    await worker_page.close()
                                except Exception:
                                    logger.debug(
                                        "Expired worker page close failed",
                                        exc_info=True,
                                    )
                            worker_page = await create_fresh_page_from_context(
                                self._shared["context"],  # type: ignore[arg-type]
                                referer_url=self._worker_referer_url(),
                            )
                            continue
                        raise

                    except (PlaywrightTimeoutError, ValueError):
                        if attempt == max_retries:
                            raise
                        backoff = min(30, 1.5**attempt) + random.uniform(0, 1.0)
                        await asyncio.sleep(backoff)
                        if worker_page is not None:
                            try:
                                await worker_page.close()
                            except Exception:
                                logger.debug(
                                    "Retry worker page close failed",
                                    exc_info=True,
                                )
                        worker_page = await create_fresh_page_from_context(
                            self._shared["context"],  # type: ignore[arg-type]
                            referer_url=self._worker_referer_url(),
                        )

            except PortalSessionRecoveryError:
                # Session recovery is shared by every worker. Let the
                # orchestrator stop the batch instead of recording the same
                # infrastructure outage against every student.
                raise
            except Exception as exc:
                logger.error("Student %s failed: %s", student_id, exc)
                await self._save_debug(
                    student_id,
                    worker_page,
                    exc,
                    debug_dir,
                    study_html=study_html,
                    timetable_html=timetable_html,
                )
                return {
                    "student_id": student_id,
                    "program": str(program or ""),
                    "ok": False,
                    "schedule_state": "",
                    "academic_year": "",
                    "term": "",
                    "error": str(exc),
                }

            finally:
                if worker_page is not None:
                    try:
                        await worker_page.close()
                    except Exception:
                        logger.debug("Worker page close failed", exc_info=True)

        return {
            "student_id": _sid_to_str(student["student_id"]),
            "program": str(student.get("program", "") or ""),
            "ok": False,
            "schedule_state": "",
            "academic_year": "",
            "term": "",
            "error": "all retries exhausted",
        }

    # ──────────────────────────────────────────────────────────
    # Helpers
    # ──────────────────────────────────────────────────────────

    def _worker_referer_url(self) -> str:
        anchor = self._shared.get("page")
        anchor_url = str(getattr(anchor, "url", "") or "").strip()
        if anchor_url and anchor_url != "about:blank":
            return anchor_url
        return str(settings.PORTAL_LOGIN_URL)

    def _read_csv(self, csv_path: str) -> list[ScrapeStudentRow]:
        with open(csv_path, encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            raw_rows = list(reader)
        expected = {"student_id", "program", "section"}
        fieldnames = reader.fieldnames or []
        if len(fieldnames) != len(set(fieldnames)):
            raise RuntimeError("CSV contains duplicate column names")
        actual = set(fieldnames)
        missing = expected - actual
        if missing:
            raise RuntimeError(f"CSV missing columns: {missing}")
        unexpected = actual - expected
        if unexpected:
            raise RuntimeError(f"CSV contains unexpected columns: {unexpected}")

        rows: list[ScrapeStudentRow] = []
        seen_students: dict[str, tuple[str, str, int]] = {}
        for line_number, raw in enumerate(raw_rows, start=2):
            if None in raw:
                raise RuntimeError(f"CSV line {line_number} has too many columns")
            student_id = _sid_to_str(raw.get("student_id", ""))
            program = str(raw.get("program") or "").strip().upper()
            section = str(raw.get("section") or "").strip().upper()
            if not student_id and not program and not section:
                # Oracle/Excel exports can contain explicit `,,` spacer rows.
                # They are not students and must never become portal requests.
                continue
            if not re.fullmatch(r"[0-9]+", student_id):
                raise RuntimeError(
                    f"CSV line {line_number} has an invalid student_id: {student_id!r}"
                )
            if not program or not section:
                raise RuntimeError(f"CSV line {line_number} must include program and section")

            previous = seen_students.get(student_id)
            if previous is not None:
                previous_program, previous_section, previous_line = previous
                if (program, section) != (previous_program, previous_section):
                    raise RuntimeError(
                        f"CSV line {line_number} conflicts with line {previous_line} "
                        f"for student {student_id}"
                    )
                continue
            seen_students[student_id] = (program, section, line_number)
            rows.append(
                {
                    "student_id": student_id,
                    "program": program,
                    "section": section,
                }
            )

        if not rows:
            raise RuntimeError("CSV contains no valid student rows")
        return rows

    async def _force_relogin(
        self,
        lock: asyncio.Lock,
        *,
        observed_generation: int,
    ) -> int:
        async with lock:
            current_generation = int(self._shared.get("session_generation", 0))
            if current_generation != observed_generation:
                return current_generation

            failed_generation = self._shared.get("session_recovery_failed_generation")
            if failed_generation == current_generation:
                failure = str(
                    self._shared.get("session_recovery_error")
                    or "Portal session recovery previously failed"
                )
                raise PortalSessionRecoveryError(failure)

            logger.warning("Session expired — performing re-login")
            previous_anchor = self._shared.get("page")
            page = None
            from core.services.portal_scraper import (
                _safe_goto,
                _safe_wait_network,
                is_logged_out_html,
                is_staff_login_success_html,
            )

            try:
                page = await self._shared["context"].new_page()  # type: ignore[attr-defined]
                await _safe_goto(page, settings.PORTAL_LOGIN_URL)
                await page.wait_for_selector('input[name="userName"]', timeout=60000)
                await page.fill('input[name="userName"]', settings.PORTAL_ADMIN_USERNAME)
                await page.fill('input[name="password"]', settings.PORTAL_ADMIN_PASSWORD)
                await page.click('input[name="submit"]')
                await _safe_wait_network(page, timeout_ms=30000)
                login_html = await safe_page_content(page)
                if is_logged_out_html(login_html) or not is_staff_login_success_html(login_html):
                    raise RuntimeError(
                        "Portal re-login failed: authenticated staff markers missing"
                    )
            except Exception as exc:
                failure = str(exc) or exc.__class__.__name__
                self._shared["session_recovery_failed_generation"] = current_generation
                self._shared["session_recovery_error"] = failure
                if page is not None:
                    try:
                        await page.close()
                    except Exception:
                        logger.debug("Failed re-login page close failed", exc_info=True)
                raise PortalSessionRecoveryError(failure) from exc

            assert page is not None
            self._shared["page"] = page
            self._shared["context"] = page.context
            new_generation = current_generation + 1
            self._shared["session_generation"] = new_generation
            self._shared.pop("session_recovery_failed_generation", None)
            self._shared.pop("session_recovery_error", None)
            if previous_anchor is not None and previous_anchor is not page:
                try:
                    await previous_anchor.close()
                except Exception:
                    logger.debug("Previous session anchor close failed", exc_info=True)
            logger.info("Re-login successful")
            return new_generation

    async def _save_debug(
        self,
        student_id: str,
        page: Any,
        exc: Exception,
        debug_dir: str,
        *,
        study_html: str = "",
        timetable_html: str = "",
    ) -> None:
        if page is None:
            html = "<NO_PAGE_CREATED>"
        else:
            try:
                html = await safe_page_content(page)
            except Exception:
                html = "<FAILED>"
        debug_path = Path(debug_dir)
        debug_path.mkdir(parents=True, exist_ok=True)
        (debug_path / f"{student_id}_debug.html").write_text(html, encoding="utf-8")
        if study_html:
            (debug_path / f"{student_id}_study.html").write_text(
                study_html,
                encoding="utf-8",
            )
        if timetable_html:
            (debug_path / f"{student_id}_timetable.html").write_text(
                timetable_html,
                encoding="utf-8",
            )
        (debug_path / f"{student_id}_debug.txt").write_text(str(exc), encoding="utf-8")
