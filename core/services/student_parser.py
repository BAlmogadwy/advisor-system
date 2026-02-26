import re

from bs4 import BeautifulSoup

from core.services.student_helpers import normalize_code


def _is_logout_or_service_page(html: str) -> bool:
    if not html:
        return True
    return (
        "<title>نظام الخدمات الالكترونية</title>" in html
        or "teachers_login.jsp" in html
        or "student_login.jsp" in html
        or "services4GraduatedStudent.do" in html
    )


_ARABIC_NUM_MAP = {
    "الأول": 1,
    "الثاني": 2,
    "الثالث": 3,
    "الرابع": 4,
    "الخامس": 5,
    "السادس": 6,
    "السابع": 7,
    "الثامن": 8,
    "التاسع": 9,
    "العاشر": 10,
}

_EN_NUM_MAP = {
    "FIRST": 1,
    "SECOND": 2,
    "THIRD": 3,
    "FOURTH": 4,
    "FIFTH": 5,
    "SIXTH": 6,
    "SEVENTH": 7,
    "EIGHTH": 8,
    "NINTH": 9,
    "TENTH": 10,
}


def _extract_level_token(text: str):
    if not text:
        return None
    t = text.strip()
    m = re.search(r"\b(\d{1,2})\b", t)
    if m:
        return int(m.group(1))
    for w in t.upper().split():
        if w in _EN_NUM_MAP:
            return _EN_NUM_MAP[w]
    for k, v in _ARABIC_NUM_MAP.items():
        if k in t:
            return v
    return None


def map_level_name_to_number(level_name):
    if not level_name:
        return None
    return _extract_level_token(level_name)


def parse_study_plan(html_content):
    if _is_logout_or_service_page(html_content):
        return []

    soup = BeautifulSoup(html_content or "", "html.parser")
    all_courses = []

    level_tables = soup.find_all("table", dir="rtl")
    if not level_tables:
        return all_courses

    for table in level_tables:
        level_header = table.find("th")
        level_name = level_header.get_text(" ", strip=True) if level_header else ""
        programme_term = map_level_name_to_number(level_name)

        rows = [r for r in table.find_all("tr") if not r.find("th")]
        if not rows:
            continue

        for row in rows:
            tds = row.find_all("td")
            if not tds:
                continue
            if any(td.get("colspan") for td in tds):
                continue
            cols = [td.get_text(" ", strip=True) for td in tds]
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

            ue = None
            if ue_raw and re.search(r"\d", ue_raw):
                try:
                    ue = int(re.sub(r"[^\d]", "", ue_raw))
                except Exception:
                    ue = None

            all_courses.append(
                {
                    "letter": letter,
                    "marks": marks,
                    "ue": ue if ue is not None else ue_raw,
                    "ue_raw": ue_raw,
                    "no": no,
                    "dept": dept,
                    "description": description,
                    "programme_term": programme_term,
                }
            )

    return all_courses


def parse_student_profile(html_content):
    """Extract student profile info from the study plan header table.

    Returns dict with keys: name, nationality, status, gpa,
    total_registered_credits, total_earned_credits.
    """
    if _is_logout_or_service_page(html_content):
        return {}

    soup = BeautifulSoup(html_content or "", "html.parser")

    profile_table = soup.find("table", class_="forumline", dir="ltr")
    if not profile_table:
        return {}

    result = {}

    field_map = {
        "Student Name":   ("name",                      str),
        "Nationality":    ("nationality",                str),
        "Student Status": ("status",                     str),
        "Student Group":  ("status",                     str),
        "T.U.Registered": ("total_registered_credits",   int),
        "T.U.Earned":     ("total_earned_credits",       int),
        "G.P.A":          ("gpa",                        float),
    }

    for row in profile_table.find_all("tr"):
        for th in row.find_all("th"):
            th_text = th.get_text(" ", strip=True)
            for label, (key, converter) in field_map.items():
                if label in th_text:
                    td = th.find_next_sibling("td")
                    if td is None:
                        continue
                    raw = td.get_text(" ", strip=True)
                    if not raw:
                        continue
                    try:
                        result[key] = converter(raw)
                    except (ValueError, TypeError):
                        result[key] = raw

    return result


def parse_timetable_info(html_content):
    """Extract current registered credits and advisor name from timetable page."""
    if _is_logout_or_service_page(html_content):
        return {}

    soup = BeautifulSoup(html_content or "", "html.parser")
    result = {}

    for th in soup.find_all("th"):
        th_text = th.get_text(" ", strip=True)

        if "مجموع الوحدات المسجلة" in th_text:
            td = th.find_next_sibling("td")
            if td:
                raw = td.get_text(" ", strip=True)
                try:
                    result["current_registered_credits"] = int(re.sub(r"[^\d]", "", raw))
                except (ValueError, TypeError):
                    pass

        if "المرشد الاكاديمي" in th_text:
            td = th.find_next_sibling("td")
            if td:
                name = td.get_text(" ", strip=True)
                if name:
                    result["advisor_name"] = name

    return result


def parse_timetable(html_content, verbose=True):
    if _is_logout_or_service_page(html_content):
        return set()

    soup = BeautifulSoup(html_content or "", "html.parser")
    timetable_courses = set()

    tables = soup.find_all("table", class_="forumline")
    if not tables:
        return timetable_courses

    course_table = None
    for t in tables:
        header_rows = t.find_all("tr")
        header_text = " ".join(hr.get_text(" ", strip=True) for hr in header_rows[:2] if hr)
        if "المادة" in header_text or "Course" in header_text or "المواد" in header_text:
            course_table = t
            break

    if course_table is None:
        course_table = tables[1] if len(tables) >= 2 else tables[0]

    rows = [r for r in course_table.find_all("tr") if not r.find("th")]
    for row in rows:
        cols = row.find_all("td")
        if len(cols) < 4:
            if any(td.get("colspan") and int(td.get("colspan")) >= 3 for td in cols):
                continue
            continue

        dept = cols[2].get_text(" ", strip=True) if len(cols) > 2 else ""
        number = cols[3].get_text(" ", strip=True) if len(cols) > 3 else ""

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
