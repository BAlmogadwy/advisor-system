from __future__ import annotations

import csv
import re
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup

DAY_ORDER = ["SUN", "MON", "TUE", "WED", "THU"]
TIME_RE = re.compile(r"^\d{1,2}:\d{2}$")
PLACEHOLDER_TEXTS = {"", "�-O", "�-<", "�-?", "�?�"}


def clean_text(t: str) -> str:
    return re.sub(r"\s+", " ", (t or "")).strip()


def read_html(path: Path) -> str:
    data = path.read_bytes()
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1256", errors="replace")


def get_bg(td: Any) -> str:  # BeautifulSoup Tag
    bg = (td.get("bgcolor") or "").strip().lower()
    if bg:
        return bg
    style = (td.get("style") or "").lower()
    m = re.search(r"background-color\s*:\s*([^;]+)", style)
    return m.group(1).strip().lower() if m else ""


def is_red(td: Any) -> bool:  # BeautifulSoup Tag
    bg = get_bg(td).replace(" ", "").lower()
    return bg in ("#ff0000", "red") or bg.startswith("rgb(255,0,0)")


def is_small_filler_td(td: Any) -> bool:  # BeautifulSoup Tag
    cs = int(td.get("colspan") or 1)
    if cs != 1:
        return False
    txt = td.get_text().replace("\xa0", "").strip()
    if txt not in PLACEHOLDER_TEXTS:
        return False
    if get_bg(td):
        return False
    return True


def keep_td(td: Any) -> bool:  # BeautifulSoup Tag
    return not is_small_filler_td(td)


def parse_course_header(text: str) -> dict[str, str]:
    t = clean_text(text)
    name = code = number = section = ""
    available_capacity = ""
    registered_count = ""

    # Primary (legacy mojibake) patterns
    m_name = re.search(r"O�O3U. OU,U.U,O�O�\s*:\s*(.*?)\s+OU,O�U.O�\s*:", t)
    m_code = re.search(r"OU,O�U.O�\s*:\s*([A-Z]+)", t)
    m_num = re.search(r"OU,O�U,U.\s*:\s*(\d+)", t)
    m_sec = re.search(r"OU,O'O1O\"Oc\s*:\s*([A-Z]\d+)", t)

    # Arabic labels (clean decoded files)
    m_name_ar = re.search(r"إسم\s*المقرر\s*:\s*(.*?)\s+الرمز\s*:", t)
    m_code_ar = re.search(r"الرمز\s*:\s*([A-Z]+)", t)
    m_num_ar = re.search(r"الرقم\s*:\s*(\d+)", t)
    m_sec_ar = re.search(r"الشعبة\s*:\s*([A-Z]\d+)", t)

    # Fallback generic patterns from mixed/garbled Arabic exports
    if not m_code and not m_code_ar:
        m_code = re.search(r"\b([A-Z]{2,6})\b", t)
    if not m_num and not m_num_ar:
        m_num = re.search(r"\b(\d{3})\b", t)
    if not m_sec and not m_sec_ar:
        m_sec = re.search(r"\b([A-Z]\d{1,4})\b", t)

    if m_name_ar:
        name = clean_text(m_name_ar.group(1))
    elif m_name:
        name = clean_text(m_name.group(1))
    if m_code_ar:
        code = m_code_ar.group(1)
    elif m_code:
        code = m_code.group(1)

    if m_num_ar:
        number = m_num_ar.group(1)
    elif m_num:
        number = m_num.group(1)

    if m_sec_ar:
        section = m_sec_ar.group(1)
    elif m_sec:
        section = m_sec.group(1)

    # Section capacity / availability + registered count (Arabic source)
    m_avail_ar = re.search(r"المتاح\s*:\s*(\d+)", t)
    if m_avail_ar:
        available_capacity = m_avail_ar.group(1)

    m_reg_ar = re.search(r"المسجلين\s*:\s*(\d+)", t)
    if m_reg_ar:
        registered_count = m_reg_ar.group(1)

    return {
        "course_name": name,
        "course_code": code,
        "course_number": number,
        "section": section,
        "available_capacity": available_capacity,
        "registered_count": registered_count,
    }


def parse_room_and_instructor(non_empty_texts: list[str]) -> tuple[str, str, str, str]:
    building = floor_wing = room = instructor = ""
    room_pat = re.compile(r"^\d{3}[A-Z]{1,4}\d{3}$")

    def is_name_like(text: str) -> bool:
        # Arabic full names are usually 3+ words and do not contain location keywords
        parts = [p for p in text.strip().split() if p]
        if len(parts) < 3:
            return False
        bad = ("جناح", "أونلاين", "اونلاين", "ONLINE", "BLACKBOARD")
        u = text.upper()
        return not any(k.upper() in u for k in bad)

    def is_location_like(text: str) -> bool:
        u = text.upper()
        return any(k in u for k in ("جناح", "أونلاين", "اونلاين", "ONLINE", "BLACKBOARD"))

    candidates = [t for t in non_empty_texts if t and not TIME_RE.match(t)]
    if candidates:
        tail = candidates[-4:]
        for item in tail:
            if room_pat.match(item):
                room = item
            elif item.isdigit() and len(item) <= 4 and not building:
                building = item
            elif "BLACKBOARD" in item.upper():
                continue
            elif is_location_like(item) and not floor_wing:
                floor_wing = item
            elif is_name_like(item) and not instructor:
                instructor = item
            elif not instructor:
                instructor = item
            elif not floor_wing:
                floor_wing = item
    return building, floor_wing, room, instructor


def extract_rows_from_oracle_html(html_path: str | Path) -> list[dict[str, str]]:
    html = read_html(Path(html_path))
    soup = BeautifulSoup(html, "html.parser")

    output_rows: list[dict[str, str]] = []
    trs = soup.find_all("tr")

    for i, tr in enumerate(trs):
        txt = clean_text(tr.get_text(" "))
        header = parse_course_header(txt)
        if not header.get("course_code") or not header.get("section"):
            continue

        nested = None
        for j in range(i + 1, min(i + 14, len(trs))):
            cand = trs[j].find("table")
            if cand and cand.find(string=re.compile(r"\d{1,2}:\d{2}")):
                nested = cand
                break
        if not nested:
            continue

        nested_trs = nested.find_all("tr")
        for ntr in nested_trs[1:]:
            tds = ntr.find_all("td")
            if not tds:
                continue

            kept = [td for td in tds if keep_td(td)]
            texts = [clean_text(td.get_text(" ").replace("\xa0", " ")) for td in kept]

            time_idx = [idx for idx, t in enumerate(texts) if TIME_RE.match(t)]
            if len(time_idx) < 2:
                continue

            s_idx, e_idx = time_idx[0], time_idx[1]
            start_time, end_time = texts[s_idx], texts[e_idx]

            day_cells = kept[e_idx + 1 : e_idx + 1 + 5]
            if len(day_cells) < 5:
                continue

            red_days = [DAY_ORDER[k] for k, dcell in enumerate(day_cells) if is_red(dcell)]
            if not red_days:
                continue

            non_empty = [t for t in texts if t]
            building, floor_wing, room, instructor = parse_room_and_instructor(non_empty)

            for day in red_days:
                output_rows.append(
                    {
                        "course_name": header.get("course_name", ""),
                        "course_code": header.get("course_code", ""),
                        "course_number": header.get("course_number", ""),
                        "section": header.get("section", ""),
                        "available_capacity": header.get("available_capacity", ""),
                        "registered_count": header.get("registered_count", ""),
                        "day": day,
                        "start_time": start_time,
                        "end_time": end_time,
                        "building": building,
                        "floor_wing": floor_wing,
                        "room": room,
                        "instructor": instructor,
                    }
                )

    return output_rows


def write_rows_to_csv(rows: list[dict[str, str]], out_path: str | Path) -> str:
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    cols = [
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
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in cols})
    return str(path)
