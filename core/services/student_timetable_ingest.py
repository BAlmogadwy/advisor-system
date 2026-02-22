from __future__ import annotations

import csv
import re
from pathlib import Path

from bs4 import BeautifulSoup

from core.models import TermSection
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
    y = re.search(r"\u0627\u0644\u0639\u0627\u0645\s*\u0627\u0644\u062f\u0631\u0627\u0633\u064a\s*:?\s*(\d{4})", txt)
    term_ar = re.search(r"\u0627\u0644\u0641\u0635\u0644\s*\u0627\u0644\u062f\u0631\u0627\u0633\u064a\s*:?\s*(\u0627\u0644\u0623\u0648\u0644|\u0627\u0644\u062b\u0627\u0646\u064a|\u0627\u0644\u062b\u0627\u0644\u062b)", txt)
    term_map = {"\u0627\u0644\u0623\u0648\u0644": "1", "\u0627\u0644\u062b\u0627\u0646\u064a": "2", "\u0627\u0644\u062b\u0627\u0644\u062b": "3"}
    year = y.group(1) if y else ""
    term = term_map.get(term_ar.group(1), "") if term_ar else ""
    return year, term


def _parse_rows(soup: BeautifulSoup) -> list[dict[str, str]]:
    target = None
    for t in soup.find_all("table", class_="forumline"):
        head = _clean(" ".join(th.get_text(" ", strip=True) for th in t.find_all("th")[:20]))
        if "\u0627\u0644\u0645\u0627\u062f\u0629" in head and "\u0634\u0639\u0628\u0629" in head and "\u0623\u062d\u062f" in head and "\u0642\u0627\u0639\u0629" in head:
            target = t
            break
    if target is None:
        return []

    out: list[dict[str, str]] = []
    current = {"course_name": "", "course_code": "", "course_number": "", "credits": "", "section": ""}

    for tr in target.find_all("tr"):
        if tr.find("th"):
            continue
        tds = tr.find_all("td")
        if len(tds) < 8:
            continue

        first_colspan = int(tds[0].get("colspan") or 1)
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

        building = _clean(tds[start_idx + 7].get_text(" ", strip=True)) if len(tds) > start_idx + 7 else ""
        floor_wing = _clean(tds[start_idx + 8].get_text(" ", strip=True)) if len(tds) > start_idx + 8 else ""
        room = _clean(tds[start_idx + 9].get_text(" ", strip=True)) if len(tds) > start_idx + 9 else ""

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


def ingest_student_timetable_html(student_id: str, timetable_html: str, report_path: str | Path | None = None) -> dict[str, object]:
    soup = BeautifulSoup(timetable_html or "", "html.parser")
    year, term = _parse_year_term(soup)
    if not year or not term:
        return {"ok": False, "error": "Unable to parse academic year/term"}

    rows = _parse_rows(soup)
    if not rows:
        return {"ok": False, "error": "No timetable rows parsed", "academic_year": year, "term": term}

    missing: list[dict[str, str]] = []
    section_ids: list[int] = []

    seen_section_key: set[tuple[str, str, str]] = set()
    for r in rows:
        key = (r["course_code"], r["course_number"], r["section"])
        if key in seen_section_key:
            continue
        seen_section_key.add(key)

        course_key = f"{r['course_code']}{r['course_number']}".replace(' ', '').upper()
        ts_id = TermSection.objects.filter(
            course_key=course_key,
            section=r["section"],
        ).values_list("id", flat=True).first()
        if ts_id is not None:
            section_ids.append(int(ts_id))
        else:
            missing.append({
                "student_id": str(student_id),
                "academic_year": year,
                "term": term,
                "course_code": r["course_code"],
                "course_number": r["course_number"],
                "section": r["section"],
            })

    replace_student_term_sections(student_id, year, term, section_ids, source="scraper_timetable")

    if report_path is not None and missing:
        p = Path(report_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        write_header = not p.exists()
        with p.open("a", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["student_id", "academic_year", "term", "course_code", "course_number", "section"])
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
    }
