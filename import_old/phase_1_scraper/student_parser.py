# phase_1_scraper/student_parser.py
from bs4 import BeautifulSoup
from utils.student_helpers import normalize_code
import re

# ============================================================
# Guards against invalid / logout HTML
# ============================================================

def _is_logout_or_service_page(html: str) -> bool:
    if not html:
        return True
    return (
        "<title>نظام الخدمات الالكترونية</title>" in html
        or "teachers_login.jsp" in html
        or "student_login.jsp" in html
        or "services4GraduatedStudent.do" in html
    )


# ============================================================
# Helpers
# ============================================================

_ARABIC_NUM_MAP = {
    "الأول": 1, "الثاني": 2, "الثالث": 3, "الرابع": 4, "الخامس": 5,
    "السادس": 6, "السابع": 7, "الثامن": 8, "التاسع": 9, "العاشر": 10
}

_EN_NUM_MAP = {
    "FIRST": 1, "SECOND": 2, "THIRD": 3, "FOURTH": 4, "FIFTH": 5,
    "SIXTH": 6, "SEVENTH": 7, "EIGHTH": 8, "NINTH": 9, "TENTH": 10
}


def _extract_level_token(text: str):
    """Return a programme term number from Arabic / English / numeric headers."""
    if not text:
        return None

    t = text.strip()

    # 1) Direct numeric
    m = re.search(r"\b(\d{1,2})\b", t)
    if m:
        return int(m.group(1))

    # 2) English ordinal words
    for w in t.upper().split():
        if w in _EN_NUM_MAP:
            return _EN_NUM_MAP[w]

    # 3) Arabic ordinal words
    for k, v in _ARABIC_NUM_MAP.items():
        if k in t:
            return v

    return None


def map_level_name_to_number(level_name):
    if not level_name:
        return None
    return _extract_level_token(level_name)


# ============================================================
# Study plan parser
# ============================================================

def parse_study_plan(html_content):
    """
    Extract all courses from the study plan page.

    Returns:
        list[dict]
    """
    if _is_logout_or_service_page(html_content):
        return []

    soup = BeautifulSoup(html_content or "", "html.parser")
    all_courses = []

    level_tables = soup.find_all("table", dir="rtl")
    if not level_tables:
        return all_courses

    for table in level_tables:
        # Extract programme term header
        level_header = table.find("th")
        level_name = level_header.get_text(" ", strip=True) if level_header else ""
        programme_term = map_level_name_to_number(level_name)

        # Skip pure header tables
        rows = [r for r in table.find_all("tr") if not r.find("th")]
        if not rows:
            continue

        for row in rows:
            tds = row.find_all("td")
            if not tds:
                continue

            # Skip separator / summary rows
            if any(td.get("colspan") for td in tds):
                continue

            cols = [td.get_text(" ", strip=True) for td in tds]

            # Must have minimum expected columns
            if len(cols) < 6:
                continue

            try:
                letter = cols[0]
                marks = cols[1]
                ue_raw = cols[2]
                no = cols[3]
                dept = cols[4]
                description = cols[5]
            except IndexError:
                continue

            if not no or not dept:
                continue

            # Normalize credit hours
            ue = None
            if ue_raw and re.search(r"\d", ue_raw):
                try:
                    ue = int(re.sub(r"[^\d]", "", ue_raw))
                except Exception:
                    ue = None

            all_courses.append({
                "letter": letter,
                "marks": marks,
                "ue": ue if ue is not None else ue_raw,
                "ue_raw": ue_raw,
                "no": no,
                "dept": dept,
                "description": description,
                "programme_term": programme_term
            })

    return all_courses


# ============================================================
# Timetable parser
# ============================================================

def parse_timetable(html_content, verbose=True):
    """
    Extract normalized course codes from the timetable page.

    Returns:
        set[str]
    """
    if _is_logout_or_service_page(html_content):
        return set()

    soup = BeautifulSoup(html_content or "", "html.parser")
    timetable_courses = set()

    tables = soup.find_all("table", class_="forumline")
    if not tables:
        return timetable_courses

    # Pick the correct course table using header keywords
    course_table = None
    for t in tables:
        header_rows = t.find_all("tr")
        header_text = " ".join(
            hr.get_text(" ", strip=True) for hr in header_rows[:2] if hr
        )
        if "المادة" in header_text or "Course" in header_text or "المواد" in header_text:
            course_table = t
            break

    if course_table is None:
        course_table = tables[1] if len(tables) >= 2 else tables[0]

    rows = [r for r in course_table.find_all("tr") if not r.find("th")]

    for row in rows:
        cols = row.find_all("td")

        # Skip separators / summaries
        if len(cols) < 4:
            if any(td.get("colspan") and int(td.get("colspan")) >= 3 for td in cols):
                continue
            continue

        dept = cols[2].get_text(" ", strip=True) if len(cols) > 2 else ""
        number = cols[3].get_text(" ", strip=True) if len(cols) > 3 else ""

        # Heuristic swap
        if dept.isdigit() and not number.isdigit():
            dept, number = number, dept

        if not dept or not number:
            continue

        raw_code = f"{dept} {number}"
        normal_code = normalize_code(raw_code)

        if verbose:
            print(f"🧪 Timetable Course Parsed: {raw_code} → {normal_code}")

        timetable_courses.add(normal_code)

    return timetable_courses
