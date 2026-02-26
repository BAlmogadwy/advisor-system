"""Batch-scrape student study plans and timetables from the university portal.

Usage:
    python manage.py scrape_students --csv data/students_list.csv
    python manage.py scrape_students --csv data/students_list.csv --concurrency 4 --save-html
"""

from __future__ import annotations

import asyncio
import csv
import logging
import os
import random
import threading
from argparse import ArgumentParser
from pathlib import Path
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from tqdm import tqdm

from core.services.portal_scraper import (
    close_browser,
    create_fresh_page_from_context,
    login_to_portal,
    navigate_to_student_study_plan,
    navigate_to_student_timetable,
    safe_page_content,
)

logger = logging.getLogger(__name__)
BASE_DIR = Path(settings.BASE_DIR)


# ------------------------------------------------------------------
# Utilities
# ------------------------------------------------------------------


def _sid_to_str(student_id: object) -> str:
    try:
        if isinstance(student_id, float) and student_id.is_integer():
            return str(int(student_id))
    except Exception:
        pass
    return str(student_id)


def _validate_study_plan(study_data: list[dict]) -> tuple[bool, str]:
    if not study_data:
        return False, "Study plan parsed empty"
    if len(study_data) < 25:
        return False, "Too few courses parsed"
    terms = [c.get("programme_term") for c in study_data if c.get("programme_term") is not None]
    if len(set(terms)) < 3:
        return False, "Too few distinct terms parsed"
    good = sum(1 for c in study_data if c.get("dept") and c.get("no") and c.get("description"))
    if good / max(len(study_data), 1) < 0.8:
        return False, "Low-quality rows"
    return True, "OK"


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
) -> dict:
    """Parse scraped HTML and persist via Django ORM.  Pure sync."""
    from core.models import Course, Student, StudentCourse
    from core.services.course_classifier import classify_courses
    from core.services.student_helpers import normalize_code
    from core.services.student_parser import (
        parse_student_profile,
        parse_study_plan,
        parse_timetable,
        parse_timetable_info,
    )
    from core.services.student_timetable_ingest import ingest_student_timetable_html

    # ── Parse ──────────────────────────────────────────────────
    study_data = parse_study_plan(study_html)
    ok, msg = _validate_study_plan(study_data)
    if not ok:
        raise ValueError(msg)

    timetable_data = parse_timetable(timetable_html, verbose=False)
    classification = classify_courses(study_data, timetable_data)
    profile = parse_student_profile(study_html)
    tt_info = parse_timetable_info(timetable_html)

    # ── Prepare data before acquiring lock ───────────────────
    sid = int(student_id)
    study_plan_codes = {normalize_code(f"{c['dept']} {c['no']}") for c in study_data}

    # ── Serialize all DB writes (SQLite single-writer) ─────
    with _db_lock:
        Student.objects.update_or_create(
            student_id=sid,
            defaults={
                "registration_no": student_id,
                "name": profile.get("name", "Unknown"),
                "nationality": profile.get("nationality", "Unknown"),
                "status": profile.get("status", "Active"),
                "gpa": profile.get("gpa", 0.0)
                if isinstance(profile.get("gpa"), int | float)
                else 0.0,
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
                "advisor_id": tt_info.get("advisor_name", ""),
            },
        )

        bridge_res = ingest_student_timetable_html(
            student_id=student_id,
            timetable_html=timetable_html,
            study_plan_codes=study_plan_codes,
        )

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
                else "not_taken"
            )

            StudentCourse.objects.update_or_create(
                student_id=sid,
                course=course_obj,
                defaults={
                    "programme_term": course.get("programme_term"),
                    "status": status,
                    "mark": None,
                    "grade": "",
                    "actual_term": "",
                },
            )

    return bridge_res


# ------------------------------------------------------------------
# Command
# ------------------------------------------------------------------


class Command(BaseCommand):
    help = "Scrape student study plans and timetables from the university portal"

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument("--csv", required=True, help="Path to students_list.csv")
        parser.add_argument("--concurrency", type=int, default=4)
        parser.add_argument("--save-html", action="store_true")
        parser.add_argument("--max-retries", type=int, default=2)
        parser.add_argument("--debug-dir", default="data/debug_failures")

    def handle(self, *args: Any, **options: Any) -> None:
        asyncio.run(self._run(options))

    # ──────────────────────────────────────────────────────────
    # Async orchestrator
    # ──────────────────────────────────────────────────────────

    async def _run(self, options: dict[str, Any]) -> None:
        csv_path = options["csv"]
        concurrency = options["concurrency"]
        max_retries = options["max_retries"]
        save_html = options["save_html"]
        debug_dir = options["debug_dir"]

        # Read CSV
        students = self._read_csv(csv_path)
        self.stdout.write(f"Loaded {len(students)} students from {csv_path}")

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
        }

        sem = asyncio.Semaphore(concurrency)
        plan_sem = asyncio.Semaphore(3)
        relogin_lock = asyncio.Lock()
        failed_ids: list[str] = []

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
                )
            )
            for s in students
        ]

        for fut in tqdm(asyncio.as_completed(tasks), total=len(tasks), desc="Scraping students"):
            try:
                result = await fut
                if result is not None:
                    failed_ids.append(result)
            except Exception as exc:
                logger.error("Task failed: %s", exc)

        # Report failures
        if failed_ids:
            fail_path = BASE_DIR / "data" / "failed_scrapes.csv"
            fail_path.parent.mkdir(exist_ok=True)
            with fail_path.open("w", encoding="utf-8", newline="") as f:
                w = csv.writer(f)
                w.writerow(["failed_student_id"])
                for sid in failed_ids:
                    w.writerow([sid])
            self.stdout.write(
                self.style.WARNING(f"{len(failed_ids)} failures saved to {fail_path}")
            )

        await close_browser(playwright_obj, browser)
        self.stdout.write(
            self.style.SUCCESS(
                f"Done. {len(students) - len(failed_ids)} succeeded, {len(failed_ids)} failed."
            )
        )

    # ──────────────────────────────────────────────────────────
    # Per-student async worker
    # ──────────────────────────────────────────────────────────

    async def _scrape_one(
        self,
        student: dict,
        sem: asyncio.Semaphore,
        plan_sem: asyncio.Semaphore,
        relogin_lock: asyncio.Lock,
        max_retries: int,
        save_html: bool,
        debug_dir: str,
    ) -> str | None:
        """Return student_id on failure, None on success."""
        async with sem:
            student_id = _sid_to_str(student["student_id"])
            program = student.get("program", "")
            section = student.get("section", "")
            worker_page = await create_fresh_page_from_context(self._shared["context"])

            try:
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

                        return None  # success

                    except RuntimeError as exc:
                        if "SESSION_LOGGED_OUT_HTML" in str(exc):
                            await self._force_relogin(relogin_lock)
                            worker_page = await create_fresh_page_from_context(
                                self._shared["context"],
                            )
                            continue
                        raise

                    except (PlaywrightTimeoutError, ValueError):
                        if attempt == max_retries:
                            raise
                        backoff = min(30, 1.5**attempt) + random.uniform(0, 1.0)
                        await asyncio.sleep(backoff)
                        worker_page = await create_fresh_page_from_context(
                            self._shared["context"],
                        )

            except Exception as exc:
                logger.error("Student %s failed: %s", student_id, exc)
                await self._save_debug(student_id, worker_page, exc, debug_dir)
                return student_id

            finally:
                try:
                    await worker_page.close()
                except Exception:
                    pass

        return student_id  # all retries exhausted

    # ──────────────────────────────────────────────────────────
    # Helpers
    # ──────────────────────────────────────────────────────────

    def _read_csv(self, csv_path: str) -> list[dict]:
        with open(csv_path, encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
        expected = {"student_id", "program", "section"}
        actual = set(rows[0].keys()) if rows else set()
        missing = expected - actual
        if missing:
            raise RuntimeError(f"CSV missing columns: {missing}")
        return rows

    async def _force_relogin(self, lock: asyncio.Lock) -> None:
        async with lock:
            logger.warning("Session expired — performing re-login")

            for p in list(self._shared["context"].pages):
                try:
                    await p.close()
                except Exception:
                    pass

            page = await self._shared["context"].new_page()
            from core.services.portal_scraper import (
                _safe_goto,
            )

            await _safe_goto(page, settings.PORTAL_LOGIN_URL)
            await page.wait_for_selector('input[name="userName"]', timeout=60000)
            await page.fill('input[name="userName"]', settings.PORTAL_ADMIN_USERNAME)
            await page.fill('input[name="password"]', settings.PORTAL_ADMIN_PASSWORD)
            await page.click('input[name="submit"]')
            await page.wait_for_selector('input[name="StudentNumber"]', timeout=60000)

            self._shared["page"] = page
            self._shared["context"] = page.context
            logger.info("Re-login successful")

    async def _save_debug(self, student_id: str, page: Any, exc: Exception, debug_dir: str) -> None:
        try:
            html = await safe_page_content(page)
        except Exception:
            html = "<FAILED>"
        debug_path = Path(debug_dir)
        debug_path.mkdir(parents=True, exist_ok=True)
        (debug_path / f"{student_id}_debug.html").write_text(html, encoding="utf-8")
        (debug_path / f"{student_id}_debug.txt").write_text(str(exc), encoding="utf-8")
