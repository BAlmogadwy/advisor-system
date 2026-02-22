import pandas as pd
import asyncio
import nest_asyncio
import os
import logging
import random
from pathlib import Path
from tqdm import tqdm
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from phase_1_scraper.student_scraper import (
    login_to_portal,
    navigate_to_student_study_plan,
    navigate_to_student_timetable,
    close_browser,
    create_fresh_page_from_context,
    safe_page_content,
)

from phase_1_scraper.student_parser import parse_study_plan, parse_timetable
from phase_2_recommender.course_classifier import classify_courses
from database.db_utils import insert_student, insert_course, insert_student_course
from config.settings import ADMIN_USERNAME, ADMIN_PASSWORD
from utils.student_helpers import normalize_code

# Additive bridge: persist student->section mappings from timetable page (does not alter legacy flow)
try:
    from core.services.student_timetable_ingest import ingest_student_timetable_html
except Exception:
    ingest_student_timetable_html = None

nest_asyncio.apply()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
BASE_DIR = Path(__file__).resolve().parents[2]


# ============================================================
# Utilities
# ============================================================

def sid_to_str(student_id):
    try:
        if isinstance(student_id, float) and student_id.is_integer():
            return str(int(student_id))
    except Exception:
        pass
    return str(student_id)


def validate_study_plan_data(study_data):
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


# ============================================================
# Batch runner
# ============================================================

async def batch_scrape_students(
    student_list_file="data/students_list.csv",
    concurrency=4,
    save_html=False,
    debug_snapshot=True,
    debug_dir="data/debug_failures",
    close_on_finish=True,
    max_retries=2,
    persist_timetable_links=True,
):
    students_df = pd.read_csv(student_list_file, dtype={"student_id": str})
    expected_cols = {"student_id", "program", "section"}
    if not expected_cols.issubset(set(students_df.columns)):
        raise RuntimeError(f"students_list.csv missing columns: {expected_cols - set(students_df.columns)}")

    students = students_df.to_dict("records")

    os.makedirs(debug_dir, exist_ok=True)
    if save_html:
        os.makedirs("data/raw_html", exist_ok=True)

    failed_ids = []

    playwright, browser, page = await login_to_portal(ADMIN_USERNAME, ADMIN_PASSWORD)
    logger.info(f"Starting batch scrape for {len(students)} students (parallel={concurrency})")

    shared = {
        "playwright": playwright,
        "browser": browser,
        "context": page.context,
        "page": page
    }

    sem = asyncio.Semaphore(concurrency)
    plan_sem = asyncio.Semaphore(3)
    relogin_lock = asyncio.Lock()

    # ========================================================
    # DEBUG SNAPSHOT
    # ========================================================

    async def save_debug_snapshot(student_id, stage, page, err):
        if not debug_snapshot:
            return
        try:
            html = await safe_page_content(page)
        except Exception:
            html = "<FAILED>"
        with open(f"{debug_dir}/{student_id}_{stage}.html", "w", encoding="utf-8") as f:
            f.write(html)
        with open(f"{debug_dir}/{student_id}_{stage}.txt", "w", encoding="utf-8") as f:
            f.write(str(err))

    # ========================================================
    # FORCE RE-LOGIN (AUTHORITATIVE)
    # ========================================================

    async def force_relogin():
        async with relogin_lock:
            logger.warning("🔒 Session expired — performing verified re-login")

            # Close all existing pages
            for p in list(shared["context"].pages):
                try:
                    await p.close()
                except Exception:
                    pass

            page = await shared["context"].new_page()

            await page.goto(
                "https://eas.taibahu.edu.sa/TaibahReg/staffLogin.do",
                wait_until="domcontentloaded"
            )

            await page.wait_for_selector('input[name="userName"]', timeout=60000)
            await page.fill('input[name="userName"]', ADMIN_USERNAME)
            await page.fill('input[name="password"]', ADMIN_PASSWORD)
            await page.click('input[name="submit"]')

            # VERIFIED login success
            await page.wait_for_selector('input[name="StudentNumber"]', timeout=60000)

            shared["page"] = page
            shared["context"] = page.context   # 🔥 CRITICAL FIX

            logger.info("✅ Verified re-login successful")

    # ========================================================
    # WORKER
    # ========================================================

    async def run_one(student):
        async with sem:
            student_id = sid_to_str(student["student_id"])
            program = student.get("program")
            section = student.get("section")

            worker_page = await create_fresh_page_from_context(shared["context"])
            stage = "start"

            try:
                for attempt in range(1, max_retries + 1):
                    try:
                        stage = f"study_plan_try{attempt}"
                        async with plan_sem:
                            study_html = await navigate_to_student_study_plan(
                                worker_page, student_id, verbose=False
                            )

                        stage = f"timetable_try{attempt}"
                        timetable_html = await navigate_to_student_timetable(
                            worker_page, student_id, verbose=False
                        )

                        study_data = parse_study_plan(study_html)
                        ok, msg = validate_study_plan_data(study_data)
                        if not ok:
                            raise ValueError(msg)

                        timetable_data = parse_timetable(timetable_html, verbose=False)
                        classification = classify_courses(study_data, timetable_data)

                        # Additive-only persistence of student timetable section mappings.
                        # Does NOT modify legacy recommendation/classification writes.
                        if persist_timetable_links and ingest_student_timetable_html is not None:
                            try:
                                runtime_dir = BASE_DIR / "runtime"
                                runtime_dir.mkdir(exist_ok=True)
                                missing_report = runtime_dir / "scrape_timetable_missing_section_links.csv"
                                bridge_res = ingest_student_timetable_html(
                                    student_id=student_id,
                                    timetable_html=timetable_html,
                                    report_path=missing_report,
                                )
                                logger.info(f"[TT-LINK] {student_id}: {bridge_res}")
                            except Exception as bridge_exc:
                                logger.warning(f"[TT-LINK] {student_id} bridge failed (non-blocking): {bridge_exc}")

                        insert_student({
                            "student_id": student_id,
                            "registration_no": student_id,
                            "name": "Unknown",
                            "nationality": "Unknown",
                            "status": "Active",
                            "gpa": 0.0,
                            "total_registered_credits": 0,
                            "total_earned_credits": 0,
                            "program": program,
                            "section": section
                        })

                        for course in study_data:
                            code = normalize_code(f"{course['dept']} {course['no']}")
                            insert_course({
                                "course_code": code,
                                "department": course["dept"],
                                "description": course["description"],
                                "credit_hours": int(course.get("ue", 0)) if str(course.get("ue", "")).isdigit() else 0
                            })
                            status = (
                                "passed" if code in classification["passed"]
                                else "studying" if code in classification["studying"]
                                else "not_taken"
                            )
                            insert_student_course(
                                student_id,
                                code,
                                {
                                    "programme_term": course.get("programme_term"),
                                    "status": status,
                                    "mark": None,
                                    "grade": None,
                                    "actual_term": None
                                }
                            )

                        if save_html:
                            with open(f"data/raw_html/{student_id}_study.html", "w", encoding="utf-8") as f:
                                f.write(study_html)
                            with open(f"data/raw_html/{student_id}_timetable.html", "w", encoding="utf-8") as f:
                                f.write(timetable_html)

                        return True

                    except RuntimeError as e:
                        if "SESSION_LOGGED_OUT_HTML" in str(e):
                            await force_relogin()
                            worker_page = await create_fresh_page_from_context(shared["context"])
                            continue
                        raise

                    except (PlaywrightTimeoutError, ValueError) as e:
                        if attempt == max_retries:
                            raise
                        backoff = min(30, (1.5 ** attempt)) + random.uniform(0, 1.0)
                        await asyncio.sleep(backoff)
                        worker_page = await create_fresh_page_from_context(shared["context"])

            except Exception as e:
                failed_ids.append(student_id)
                await save_debug_snapshot(student_id, stage, worker_page, e)

            finally:
                try:
                    await worker_page.close()
                except Exception:
                    pass

    # ========================================================
    # RUN
    # ========================================================

    tasks = [asyncio.create_task(run_one(s)) for s in students]

    for fut in tqdm(asyncio.as_completed(tasks), total=len(tasks), desc="Scraping students"):
        try:
            await fut
        except Exception as e:
            logger.error(f"Task failed: {e}")

    if failed_ids:
        pd.DataFrame({"failed_student_id": failed_ids}).to_csv("data/failed_scrapes.csv", index=False)

    if close_on_finish:
        await close_browser(shared["playwright"], shared["browser"])

    logger.info("Batch scraping complete.")


# ============================================================
# ENTRY
# ============================================================

if __name__ == "__main__":
    asyncio.run(
        batch_scrape_students(
            student_list_file="data/students_list.csv",
            concurrency=4,
            save_html=True,
            debug_snapshot=True,
            close_on_finish=True
        )
    )
