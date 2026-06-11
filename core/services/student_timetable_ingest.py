from __future__ import annotations

import csv
import re
from datetime import UTC, datetime
from pathlib import Path

from bs4 import BeautifulSoup

from core.models import Course, StudentCourse, TermSection, TermSectionMeeting
from core.services.student_sections import replace_student_term_sections

DAY_COLS = ["SUN", "MON", "TUE", "WED", "THU"]


def _clean(t: str) -> str:
    return re.sub(r"\s+", " ", (t or "")).strip()


def _normalize_ar_text(t: str) -> str:
    # remove tatweel and collapse spaces for robust matching
    t = (t or "").replace("\u0640", "")
    return re.sub(r"\s+", " ", t).strip()


def _parse_year_term(soup: BeautifulSoup) -> tuple[str, str]:
    txt = _normalize_ar_text(soup.get_text(" ", strip=True))
    y = re.search(
        r"\u0627\u0644\u0639\u0627\u0645\s*\u0627\u0644\u062f\u0631\u0627\u0633\u064a\s*:?\s*(\d{4})",
        txt,
    )
    term_ar = re.search(
        r"\u0627\u0644\u0641\u0635\u0644\s*\u0627\u0644\u062f\u0631\u0627\u0633\u064a\s*:?\s*(\u0627\u0644\u0623\u0648\u0644|\u0627\u0644\u062b\u0627\u0646\u064a|\u0627\u0644\u062b\u0627\u0644\u062b)",
        txt,
    )
    term_map = {
        "\u0627\u0644\u0623\u0648\u0644": "1",
        "\u0627\u0644\u062b\u0627\u0646\u064a": "2",
        "\u0627\u0644\u062b\u0627\u0644\u062b": "3",
    }
    year = y.group(1) if y else ""
    term = term_map.get(term_ar.group(1), "") if term_ar else ""
    return year, term


def _parse_rows(soup: BeautifulSoup) -> list[dict[str, str]]:
    target = None
    for t in soup.find_all("table", class_="forumline"):
        head = _clean(" ".join(th.get_text(" ", strip=True) for th in t.find_all("th")[:20]))
        if (
            "\u0627\u0644\u0645\u0627\u062f\u0629" in head
            and "\u0634\u0639\u0628\u0629" in head
            and "\u0623\u062d\u062f" in head
            and "\u0642\u0627\u0639\u0629" in head
        ):
            target = t
            break
    if target is None:
        return []

    out: list[dict[str, str]] = []
    current = {
        "course_name": "",
        "course_code": "",
        "course_number": "",
        "credits": "",
        "section": "",
    }

    for tr in target.find_all("tr"):
        if tr.find("th"):
            continue
        tds = tr.find_all("td")
        if len(tds) < 8:
            continue

        first_colspan = int(tds[0].get("colspan") or 1)  # type: ignore[arg-type]
        is_cont = first_colspan >= 6

        if not is_cont:
            if len(tds) < 16:
                continue
            current = {
                "course_name": _clean(tds[1].get_text(" ", strip=True)),
                "course_code": _clean(tds[2].get_text(" ", strip=True)).upper(),
                "course_number": _clean(tds[3].get_text(" ", strip=True)),
                "credits": _clean(tds[4].get_text(" ", strip=True)),
                "section": _clean(tds[5].get_text(" ", strip=True)).upper(),
            }
            start_idx = 6
        else:
            # row starts after colspan=6
            if len(tds) < 10:
                continue
            start_idx = 1

        start_time = _clean(tds[start_idx].get_text(" ", strip=True))
        end_time = _clean(tds[start_idx + 1].get_text(" ", strip=True))

        day_cells = tds[start_idx + 2 : start_idx + 7]
        days: list[str] = []
        for i, dc in enumerate(day_cells):
            has_mark = dc.find("img") is not None
            if has_mark:
                days.append(DAY_COLS[i])

        building = (
            _clean(tds[start_idx + 7].get_text(" ", strip=True)) if len(tds) > start_idx + 7 else ""
        )
        floor_wing = (
            _clean(tds[start_idx + 8].get_text(" ", strip=True)) if len(tds) > start_idx + 8 else ""
        )
        room = (
            _clean(tds[start_idx + 9].get_text(" ", strip=True)) if len(tds) > start_idx + 9 else ""
        )

        for d in days:
            out.append(
                {
                    **current,
                    "day": d,
                    "start_time": start_time,
                    "end_time": end_time,
                    "building": building,
                    "floor_wing": floor_wing,
                    "room": room,
                }
            )

    return out


def _ensure_external_course(
    course_key: str,
    course_code: str,
    course_number: str,
    course_name: str,
    credits_str: str,
) -> Course:
    """Get or create a Course entry for an external (non-plan) course."""
    course_obj = Course.objects.filter(course_code=course_key).first()
    if course_obj is not None:
        return course_obj

    try:
        credit_hours = int(re.sub(r"[^\d]", "", credits_str)) if credits_str else 0
    except (ValueError, TypeError):
        credit_hours = 0

    return Course.objects.create(
        course_code=course_key,
        department=course_code,
        description=course_name,
        credit_hours=credit_hours,
        is_external=True,
    )


def _ensure_term_section(
    course_key: str,
    course_code: str,
    course_number: str,
    course_name: str,
    section: str,
    meetings: list[dict[str, str]],
    source_tag: str = "scraper_timetable",
) -> int:
    """Get or create a TermSection and ensure ALL its meetings exist.

    Meetings are ensured (idempotent get_or_create) for pre-existing sections
    too, so re-ingesting a fuller timetable backfills meetings an earlier partial
    scrape missed — a section is never frozen at its first-seen meeting.
    """
    now_str = datetime.now(UTC).isoformat()
    # Look up global (non-scenario) sections for imported/scraped data
    ts = TermSection.objects.filter(
        scenario__isnull=True, course_key=course_key, section=section
    ).first()
    if ts is None:
        ts = TermSection.objects.create(
            source_tag=source_tag,
            course_name=course_name,
            course_code=course_code,
            course_number=course_number,
            course_key=course_key,
            section=section,
            source_file=f"timetable_ingest_{source_tag}",
            created_at=now_str,
            updated_at=now_str,
        )

    for m in meetings:
        TermSectionMeeting.objects.get_or_create(
            term_section=ts,
            day=m.get("day", ""),
            start_time=m.get("start_time", ""),
            end_time=m.get("end_time", ""),
            room=m.get("room", ""),
            defaults={
                "building": m.get("building", ""),
                "floor_wing": m.get("floor_wing", ""),
                "instructor": "",
                "created_at": now_str,
                "updated_at": now_str,
            },
        )

    return ts.id


def _ensure_student_course_studying(student_id: str | int, course: Course) -> None:
    """Create a StudentCourse with status='studying' if one doesn't already exist."""
    from core.models import Student

    sid = int(student_id)
    if not Student.objects.filter(student_id=sid).exists():
        return

    exists = StudentCourse.objects.filter(
        student_id=sid,
        course=course,
        status="studying",
    ).exists()
    if not exists:
        # Also skip if student already passed this course
        if StudentCourse.objects.filter(student_id=sid, course=course, status="passed").exists():
            return
        StudentCourse.objects.create(
            student_id=sid,
            course=course,
            programme_term=None,
            status="studying",
            grade="",
            mark=None,
            actual_term="",
        )


def ingest_student_timetable_html(
    student_id: str,
    timetable_html: str,
    report_path: str | Path | None = None,
    study_plan_codes: set[str] | None = None,
) -> dict[str, object]:
    soup = BeautifulSoup(timetable_html or "", "html.parser")
    year, term = _parse_year_term(soup)
    if not year or not term:
        return {"ok": False, "error": "Unable to parse academic year/term"}

    rows = _parse_rows(soup)
    if not rows:
        return {
            "ok": False,
            "error": "No timetable rows parsed",
            "academic_year": year,
            "term": term,
        }

    missing: list[dict[str, str]] = []
    section_ids: list[int] = []
    external_created: list[str] = []

    # ── Pass 1 — accumulate the COMPLETE meeting list per section ────────────
    # One parsed row == one meeting (a course meeting Sun+Tue yields two rows;
    # continuation rows add more). Every row for a section must be gathered
    # BEFORE the section is created, otherwise the section is created on its
    # first row and the remaining meetings are silently dropped.
    section_meetings: dict[tuple[str, str, str], list[dict[str, str]]] = {}
    course_meta: dict[str, dict[str, str]] = {}
    section_course_key: dict[tuple[str, str, str], str] = {}
    for r in rows:
        key = (r["course_code"], r["course_number"], r["section"])
        course_key = f"{r['course_code']}{r['course_number']}".replace(" ", "").upper()
        section_course_key[key] = course_key
        section_meetings.setdefault(key, []).append(
            {
                "day": r.get("day", ""),
                "start_time": r.get("start_time", ""),
                "end_time": r.get("end_time", ""),
                "building": r.get("building", ""),
                "floor_wing": r.get("floor_wing", ""),
                "room": r.get("room", ""),
            }
        )
        # Store course metadata from the first row per course_key
        if course_key not in course_meta:
            course_meta[course_key] = {
                "course_code": r["course_code"],
                "course_number": r["course_number"],
                "course_name": r.get("course_name", ""),
                "credits": r.get("credits", ""),
                "section": r["section"],
            }

    # ── Pass 2 — create/link each section with its full meeting list ─────────
    for key, meetings in section_meetings.items():
        course_code, course_number, section = key
        course_key = section_course_key[key]
        meta = course_meta[course_key]

        # Determine if this is an external course (not in the study plan)
        if study_plan_codes is not None:
            is_external = course_key not in study_plan_codes
        else:
            from core.models import ProgrammeRequirement

            is_external = not ProgrammeRequirement.objects.filter(
                course_code=course_key,
            ).exists()

        if is_external:
            course_obj = _ensure_external_course(
                course_key=course_key,
                course_code=meta["course_code"],
                course_number=meta["course_number"],
                course_name=meta["course_name"],
                credits_str=meta["credits"],
            )
            _ensure_student_course_studying(student_id, course_obj)
            if course_key not in external_created:
                external_created.append(course_key)

        ts_id = _ensure_term_section(
            course_key=course_key,
            course_code=course_code,
            course_number=course_number,
            course_name=meta["course_name"],
            section=section,
            meetings=meetings,
            source_tag="external" if is_external else "scraper_timetable",
        )
        section_ids.append(ts_id)

    replace_student_term_sections(student_id, year, term, section_ids, source="scraper_timetable")

    if report_path is not None and missing:
        p = Path(report_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        write_header = not p.exists()
        with p.open("a", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(
                f,
                fieldnames=[
                    "student_id",
                    "academic_year",
                    "term",
                    "course_code",
                    "course_number",
                    "section",
                ],
            )
            if write_header:
                w.writeheader()
            for m in missing:
                w.writerow(m)

    return {
        "ok": True,
        "academic_year": year,
        "term": term,
        "parsed_rows": len(rows),
        "mapped_sections": len(section_ids),
        "missing_links": len(missing),
        "external_courses_created": len(external_created),
        "external_courses": external_created,
    }
