from __future__ import annotations

import csv
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import TypedDict

from bs4 import BeautifulSoup
from bs4.element import Tag

from core.models import Course, StudentCourse, TermSection, TermSectionMeeting
from core.services.student_helpers import normalize_code
from core.services.student_sections import replace_student_term_sections

DAY_COLS = ["SUN", "MON", "TUE", "WED", "THU"]


class ValidatedTimetableResponse(TypedDict):
    """Pure, validated timetable data that is safe to persist.

    Validation is intentionally separate from ingestion so callers can reject
    malformed portal responses before the first database write. The portal's
    summer/no-timetable response is accepted only through an explicit caller
    opt-in after that caller has independently verified the study-plan identity.
    """

    student_id: str
    academic_year: str
    term: str
    current_registered_credits: int
    rows: list[dict[str, str]]
    #: Courses the portal shows as REGISTERED but places on no weekday: a Program
    #: Elective placeholder, a graduation project, a course taught elsewhere. They
    #: carry a section and credits — and the portal counts those credits in its own
    #: declared total — but there is no meeting to draw. See the validator.
    unscheduled: list[dict[str, str]]
    schedule_state: str


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
        r"\u0627\u0644\u0641\u0635\u0644\s*\u0627\u0644\u062f\u0631\u0627\u0633\u064a\s*:?\s*(\u0627\u0644\u0623\u0648\u0644|\u0627\u0644\u062b\u0627\u0646\u064a|\u0627\u0644\u062b\u0627\u0644\u062b|\u0627\u0644\u0635\u064a\u0641\u064a)",
        txt,
    )
    term_map = {
        "\u0627\u0644\u0623\u0648\u0644": "1",
        "\u0627\u0644\u062b\u0627\u0646\u064a": "2",
        "\u0627\u0644\u062b\u0627\u0644\u062b": "3",
        # The university exposes summer as the third registrable term across
        # the rest of this project (term values are constrained to 1/2/3).
        "\u0627\u0644\u0635\u064a\u0641\u064a": "3",
    }
    year = y.group(1) if y else ""
    term = term_map.get(term_ar.group(1), "") if term_ar else ""
    return year, term


def _find_timetable_table(soup: BeautifulSoup) -> Tag | None:
    for t in soup.find_all("table", class_="forumline"):
        if not isinstance(t, Tag):
            continue
        head = _clean(" ".join(th.get_text(" ", strip=True) for th in t.find_all("th")[:20]))
        if (
            "\u0627\u0644\u0645\u0627\u062f\u0629" in head
            and "\u0634\u0639\u0628\u0629" in head
            and "\u0623\u062d\u062f" in head
            and "\u0642\u0627\u0639\u0629" in head
        ):
            return t
    return None


def _parse_rows(soup: BeautifulSoup) -> list[dict[str, str]]:
    target = _find_timetable_table(soup)
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
        if not isinstance(tr, Tag):
            continue
        if tr.find("th"):
            continue
        tds = [td for td in tr.find_all("td") if isinstance(td, Tag)]
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


def _parse_student_id(soup: BeautifulSoup) -> str:
    label = "\u0631\u0642\u0645 \u0627\u0644\u0637\u0627\u0644\u0628"
    for th in soup.find_all("th"):
        if label not in _normalize_ar_text(th.get_text(" ", strip=True)):
            continue
        td = th.find_next_sibling("td")
        if td is None:
            continue
        match = re.search(r"\d{5,}", _clean(td.get_text(" ", strip=True)))
        if match:
            return match.group(0)
    return ""


def _parse_clock(value: str, label: str) -> datetime:
    try:
        return datetime.strptime(value, "%H:%M")
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Timetable {label} is invalid: {value!r}") from exc


def _validate_structured_rows(
    soup: BeautifulSoup,
    expected_registered_credits: int,
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Validate every physical timetable row, including continuations.

    Returns ``(meeting_rows, unscheduled_registrations)``. The second list is not
    an error channel: it is the courses the portal registers on no weekday.
    """
    target = _find_timetable_table(soup)
    if target is None:
        raise ValueError("Timetable course table could not be identified")

    current_course_key = ""
    # course_key -> (section, credits, course_code, course_number, course_name)
    registered_courses: dict[str, tuple[str, int, str, str, str]] = {}
    courses_with_meetings: set[str] = set()
    meeting_count = 0

    for tr in target.find_all("tr"):
        if not isinstance(tr, Tag):
            continue
        if tr.find("th"):
            continue
        tds = [td for td in tr.find_all("td") if isinstance(td, Tag)]
        if not tds:
            continue

        try:
            raw_colspan = tds[0].get("colspan")
            if raw_colspan is not None and not isinstance(raw_colspan, str | int):
                raise ValueError
            first_colspan = int(raw_colspan or 1)
        except (TypeError, ValueError) as exc:
            raise ValueError("Timetable row has an invalid column span") from exc
        is_continuation = first_colspan >= 6

        if is_continuation:
            if not current_course_key or len(tds) < 10:
                raise ValueError("Timetable contains an invalid continuation row")
            start_idx = 1
        else:
            if len(tds) < 16:
                raise ValueError("Timetable contains an incomplete course row")

            course_name = _clean(tds[1].get_text(" ", strip=True))
            course_code = _clean(tds[2].get_text(" ", strip=True)).upper()
            course_number = _clean(tds[3].get_text(" ", strip=True))
            credits_text = _clean(tds[4].get_text(" ", strip=True))
            section = _clean(tds[5].get_text(" ", strip=True)).upper()
            if not all((course_name, course_code, course_number, credits_text, section)):
                raise ValueError("Timetable contains incomplete course metadata")
            if not credits_text.isdigit() or int(credits_text) <= 0:
                raise ValueError(
                    f"Timetable course {course_code}{course_number} has invalid credits"
                )

            current_course_key = normalize_code(f"{course_code}{course_number}")
            if not current_course_key:
                raise ValueError("Timetable contains an invalid course code")
            credits = int(credits_text)
            previous = registered_courses.get(current_course_key)
            if previous is not None and previous[:2] != (section, credits):
                raise ValueError(
                    f"Timetable registers {current_course_key} more than once inconsistently"
                )
            registered_courses[current_course_key] = (
                section,
                credits,
                course_code,
                course_number,
                course_name,
            )
            start_idx = 6

        if len(tds) <= start_idx + 9:
            raise ValueError("Timetable contains an incomplete meeting row")
        start_time = _clean(tds[start_idx].get_text(" ", strip=True))
        end_time = _clean(tds[start_idx + 1].get_text(" ", strip=True))
        if _parse_clock(start_time, "start time") >= _parse_clock(end_time, "end time"):
            raise ValueError("Timetable meeting start time must be before end time")

        day_cells = tds[start_idx + 2 : start_idx + 7]
        marked_days = sum(cell.find("img") is not None for cell in day_cells)
        if len(day_cells) != len(DAY_COLS):
            raise ValueError("Timetable meeting row has incomplete day columns")
        if marked_days == 0:
            # Some valid portal schedules include an unmarked presentation row
            # followed by a marked continuation for the same course.  It is not
            # persisted as a meeting, but the course must have at least one
            # independently usable meeting elsewhere in the response.
            continue
        courses_with_meetings.add(current_course_key)
        meeting_count += marked_days

    if not registered_courses:
        raise ValueError("Timetable contains no structured meeting rows")

    # A course the portal registers but places on NO weekday used to be refused as
    # a malformed response. It is not: it is how the portal shows a Program
    # Elective placeholder («مقرر اختياري برنامج»), a graduation project, and a
    # course taught elsewhere — a real section, real credits, and deliberately no
    # day. Refusing it made every student holding one unscrapable, which on this
    # programme is a large share of them.
    #
    # NOTHING about the completeness proof is weakened by accepting it. That proof
    # is the credit reconciliation below: the portal's own declared total must
    # equal the sum over unique registered courses, and an unscheduled course is
    # counted there exactly like a scheduled one. Days were never what made the
    # response provably complete.
    unscheduled_courses = sorted(set(registered_courses) - courses_with_meetings)
    unscheduled = [
        {
            "course_code": registered_courses[course_key][2],
            "course_number": registered_courses[course_key][3],
            "course_name": registered_courses[course_key][4],
            "section": registered_courses[course_key][0],
            "credits": str(registered_courses[course_key][1]),
        }
        for course_key in unscheduled_courses
    ]

    declared_sum = sum(entry[1] for entry in registered_courses.values())
    if declared_sum != expected_registered_credits:
        raise ValueError(
            "Timetable registered-credit total does not match its unique course rows "
            f"({expected_registered_credits} declared, {declared_sum} parsed)"
        )

    rows = _parse_rows(soup)
    if len(rows) != meeting_count:
        raise ValueError("Timetable meeting rows could not be parsed completely")
    return rows, unscheduled


def validate_timetable_response(
    timetable_html: str,
    *,
    expected_student_id: str,
    expected_registered_credits: object,
    allow_confirmed_empty: bool = False,
) -> ValidatedTimetableResponse:
    """Parse the structural timetable fields without touching the database.

    Raises ``ValueError`` when the response cannot prove that it is a complete
    current timetable.  The scraper performs this check together with its
    profile/study-plan checks before entering the database write section.
    """
    if not str(timetable_html or "").strip():
        raise ValueError("Timetable response is empty")

    requested_student_id = str(expected_student_id or "").strip()
    if not requested_student_id.isdigit():
        raise ValueError("Requested student ID is invalid")

    soup = BeautifulSoup(timetable_html, "html.parser")
    response_text = _normalize_ar_text(soup.get_text(" ", strip=True))
    portal_no_record_marker = (
        "\u0631\u0642\u0645 \u0627\u0644\u0637\u0627\u0644\u0628 \u0628\u0647 \u062e\u0637\u0623"
        in response_text
    )
    if portal_no_record_marker:
        # Chrome verification during the summer period confirmed that the
        # timetable service uses this misleading message when a real student
        # has no current timetable. A marker embedded in a purported timetable
        # is contradictory and remains invalid. The caller may opt in only
        # after independently matching the study-plan identity to this request.
        has_timetable_evidence = bool(
            _find_timetable_table(soup) or _parse_student_id(soup) or any(_parse_year_term(soup))
        )
        page_title = _normalize_ar_text(
            soup.title.get_text(" ", strip=True) if soup.title is not None else ""
        )
        is_timetable_response = page_title in {
            "الجدول الدراسي لطالب",
            "الجدول الدراسى لطالب",
        }
        if not allow_confirmed_empty or has_timetable_evidence or not is_timetable_response:
            raise ValueError("Portal rejected the student ID")
        return {
            "student_id": requested_student_id,
            "academic_year": "",
            "term": "",
            "current_registered_credits": 0,
            "rows": [],
            "unscheduled": [],
            "schedule_state": "confirmed_empty_current_schedule",
        }

    parsed_student_id = _parse_student_id(soup)
    if not parsed_student_id:
        raise ValueError("Timetable student ID could not be parsed")
    if parsed_student_id != requested_student_id:
        raise ValueError(
            f"Timetable belongs to student {parsed_student_id}, "
            f"not requested student {requested_student_id}"
        )

    if (
        isinstance(expected_registered_credits, bool)
        or not isinstance(expected_registered_credits, int)
        or expected_registered_credits < 0
    ):
        raise ValueError("Expected registered credits must be a non-negative integer")

    year, term = _parse_year_term(soup)
    if not year or not term:
        raise ValueError("Timetable academic year or term could not be parsed")

    rows, unscheduled = _validate_structured_rows(soup, expected_registered_credits)

    return {
        "student_id": parsed_student_id,
        "academic_year": year,
        "term": term,
        "current_registered_credits": expected_registered_credits,
        "rows": rows,
        "unscheduled": unscheduled,
        "schedule_state": "complete_schedule",
    }


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
    meetings: list[dict[str, str]] | None,
    source_tag: str = "scraper_timetable",
) -> int:
    """Get or create a TermSection and replace its current meeting snapshot.

    ``meetings=None`` means LEAVE THE SNAPSHOT ALONE, and it is not the same as
    ``[]``. An unscheduled registration — a Program Elective placeholder, a
    graduation project — tells us the STUDENT is registered on no weekday; it says
    nothing about the section. Passing ``[]`` there would compute an empty desired
    set and delete every meeting the section has, so a scrape of one student would
    erase timetable data another source had imported.
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

    def meeting_key(meeting: object) -> tuple[str, str, str, str]:
        if isinstance(meeting, dict):
            return (
                meeting.get("day", ""),
                meeting.get("start_time", ""),
                meeting.get("end_time", ""),
                meeting.get("room", ""),
            )
        return (
            str(getattr(meeting, "day", "") or ""),
            str(getattr(meeting, "start_time", "") or ""),
            str(getattr(meeting, "end_time", "") or ""),
            str(getattr(meeting, "room", "") or ""),
        )

    if meetings is None:
        return int(ts.id)

    desired = {meeting_key(meeting): meeting for meeting in meetings}
    existing = list(TermSectionMeeting.objects.filter(term_section=ts))
    stale_ids = [meeting.pk for meeting in existing if meeting_key(meeting) not in desired]
    if stale_ids:
        TermSectionMeeting.objects.filter(pk__in=stale_ids).delete()

    for key, meeting in desired.items():
        matching_ids = [row.pk for row in existing if meeting_key(row) == key]
        if matching_ids:
            # Timetable pages do not carry instructor identity. Keep it on
            # matching imported rows while refreshing location metadata.
            TermSectionMeeting.objects.filter(pk__in=matching_ids).update(
                building=meeting.get("building", ""),
                floor_wing=meeting.get("floor_wing", ""),
                updated_at=now_str,
            )
            continue
        TermSectionMeeting.objects.create(
            term_section=ts,
            day=meeting.get("day", ""),
            start_time=meeting.get("start_time", ""),
            end_time=meeting.get("end_time", ""),
            room=meeting.get("room", ""),
            building=meeting.get("building", ""),
            floor_wing=meeting.get("floor_wing", ""),
            instructor="",
            created_at=now_str,
            updated_at=now_str,
        )

    return ts.id


def _ensure_student_course_studying(student_id: str | int, course: Course) -> None:
    """Ensure a current external course is studying without erasing its history."""
    from core.models import Student

    sid = int(student_id)
    if not Student.objects.filter(student_id=sid).exists():
        return

    student_course, created = StudentCourse.objects.get_or_create(
        student_id=sid,
        course=course,
        defaults={
            "programme_term": None,
            "status": "studying",
        },
    )
    if not created and student_course.status not in {"passed", "studying"}:
        student_course.status = "studying"
        student_course.save(update_fields=["status"])


def ingest_student_timetable_html(
    student_id: str,
    timetable_html: str,
    report_path: str | Path | None = None,
    study_plan_codes: set[str] | None = None,
    validated_response: ValidatedTimetableResponse | None = None,
) -> dict[str, object]:
    if validated_response is None:
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
    else:
        year = validated_response["academic_year"]
        term = validated_response["term"]
        rows = validated_response["rows"]

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

    # ── Pass 3 — registrations the portal places on no weekday ──────────────
    # They carry a section and credits and are counted in the portal's own
    # declared total, so leaving them out would under-record what the student is
    # registered in — and the student portal already has a place to show them
    # («مقررات بدون وقت محدد» / "Courses without a scheduled time"), because
    # `get_student_term_baseline` emits a row for a section with no meetings.
    for entry in validated_response.get("unscheduled", []) if validated_response else []:
        course_key = f"{entry['course_code']}{entry['course_number']}".replace(" ", "").upper()
        if not course_key:
            continue
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
                course_code=entry["course_code"],
                course_number=entry["course_number"],
                course_name=entry.get("course_name", ""),
                credits_str=entry.get("credits", ""),
            )
            _ensure_student_course_studying(student_id, course_obj)
            if course_key not in external_created:
                external_created.append(course_key)
        section_ids.append(
            _ensure_term_section(
                course_key=course_key,
                course_code=entry["course_code"],
                course_number=entry["course_number"],
                course_name=entry.get("course_name", ""),
                section=entry.get("section", ""),
                # None, not []: see `_ensure_term_section`.
                meetings=None,
                source_tag="external" if is_external else "scraper_timetable",
            )
        )

    replace_result = replace_student_term_sections(
        student_id,
        year,
        term,
        section_ids,
        source="scraper_timetable",
        replace_source_across_terms="scraper_timetable",
    )

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
        "schedule_state": "complete_schedule",
        "academic_year": year,
        "term": term,
        "parsed_rows": len(rows),
        "unscheduled_registrations": len(
            validated_response.get("unscheduled", []) if validated_response else []
        ),
        "mapped_sections": int(replace_result.get("inserted") or 0),
        "excluded_other_branch_sections": int(replace_result.get("excluded_other_branch") or 0),
        "missing_links": len(missing),
        "external_courses_created": len(external_created),
        "external_courses": external_created,
    }
