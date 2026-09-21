"""Recorded teaching sections for the exact population of an exam.

Course enrolment remains authoritative. Section evidence describes that same
population; incomplete mappings must never remove students or invent sections.
"""

from __future__ import annotations

import hashlib
from collections import Counter, defaultdict
from types import SimpleNamespace

from core.models import Student, StudentTermSection, TermSectionMeeting
from core.services.group_availability import resolve_current_term
from core.services.student_helpers import normalize_code
from core.services.student_sections import (
    OTHER_BRANCH_SECTION_COHORT,
    _section_course_key,
    section_gender,
)

EXAM_ENROLLMENT_SOURCE = "scraper_timetable"


def exam_timetable_links():
    """Actual scraped registrations in the latest scraped term, without fallback.

    Keep source and term selection shared by exam enrollment and section lookup.
    The section catalogue's source_tag is deliberately irrelevant: a scraper
    link can refer to a catalogue section originally created by a plan import.
    """
    year, term = resolve_current_term(source=EXAM_ENROLLMENT_SOURCE, global_sections_only=True)
    links = StudentTermSection.objects.filter(
        source=EXAM_ENROLLMENT_SOURCE,
        academic_year=year,
        term=term,
        term_section__scenario__isnull=True,
    )
    if not year or not term:
        links = links.none()
    return links, year, term


def _membership_fingerprint(student_ids: set[int]) -> str:
    return hashlib.sha256(
        ",".join(str(sid) for sid in sorted(student_ids)).encode("ascii")
    ).hexdigest()


def resolve_exam_section_enrollment(
    enrolled_sets: dict[str, set[int]],
    *,
    course_meta: dict[str, dict] | None = None,
    section_by_student: dict[int, str] | None = None,
    program_by_student: dict[int, str] | None = None,
    operations_sections: dict[str, list[dict]] | None = None,
) -> dict[str, list[dict]]:
    """Match registrar links by student and source code inside each identity.

    Arabic catalogue names need not equal a plan's English name. The caller's
    canonical course population already disambiguates shared course codes.
    Forecast, working/scenario and other-campus links cannot supply evidence of
    a registered section. Multiple real links are reported, never guessed away.
    """
    all_students = {int(sid) for sids in enrolled_sets.values() for sid in sids}
    if not all_students:
        if operations_sections is not None:
            operations_sections.update({code: [] for code in enrolled_sets})
        return {code: [] for code in enrolled_sets}
    course_meta = course_meta or {}
    if section_by_student is None:
        section_by_student = dict(
            Student.objects.filter(student_id__in=all_students).values_list("student_id", "section")
        )
    if operations_sections is not None and program_by_student is None:
        program_by_student = dict(
            Student.objects.filter(student_id__in=all_students).values_list("student_id", "program")
        )
    source_codes = {
        display: normalize_code(
            course_meta.get(display, {}).get("source_course_code")
            or (display.rsplit(" (", 1)[0] if display.endswith(")") else display)
        )
        for display in enrolled_sets
    }
    source_links, year, term = exam_timetable_links()
    candidates: dict[tuple[int, str], dict[int, dict]] = defaultdict(dict)
    if year and term:
        links = source_links.filter(
            student_id__in=all_students,
        ).values(
            "student_id",
            "term_section_id",
            "term_section__course_key",
            "term_section__section",
            "term_section__course_code",
            "term_section__course_number",
        )
        wanted = set(source_codes.values())
        for row in links:
            code = _section_course_key(
                SimpleNamespace(
                    **{
                        key: row[f"term_section__{key}"]
                        for key in ("course_key", "course_code", "course_number")
                    }
                )
            )
            label = str(row["term_section__section"] or "")
            allowed_gender = section_gender(label)
            if (
                code not in wanted
                or not label.strip()
                or allowed_gender == OTHER_BRANCH_SECTION_COHORT
            ):
                continue
            sid = int(row["student_id"])
            cohort = str(section_by_student.get(sid, "") or "").strip().upper()
            if cohort not in {"M", "F"} or (allowed_gender and allowed_gender != cohort):
                continue
            candidates[(sid, code)][row["term_section_id"]] = {
                "term_section_id": row["term_section_id"],
                "section": label,
            }

    section_ids = {section_id for rows in candidates.values() for section_id in rows}
    room_counts: dict[int, Counter] = defaultdict(Counter)
    instructors: dict[int, set[str]] = defaultdict(set)
    for section_id, room, instructor in TermSectionMeeting.objects.filter(
        term_section_id__in=section_ids
    ).values_list("term_section_id", "room", "instructor"):
        if str(room or "").strip():
            room_counts[section_id][str(room).strip()] += 1
        if str(instructor or "").strip():
            instructors[section_id].add(str(instructor).strip())
    preferred = {
        section_id: min(counts, key=lambda room: (-counts[room], room))
        for section_id, counts in room_counts.items()
    }

    result: dict[str, list[dict]] = {}
    for display, student_ids in enrolled_sets.items():
        groups: dict[tuple[str, str], dict] = {}
        members: dict[tuple[str, str], set[int]] = defaultdict(set)
        for raw_sid in sorted(student_ids):
            sid = int(raw_sid)
            cohort = str(section_by_student.get(sid, "") or "").strip().upper()
            matches = candidates.get((sid, source_codes[display]), {})
            status = "mapped" if len(matches) == 1 else "ambiguous" if matches else "missing"
            match = next(iter(matches.values())) if status == "mapped" else {}
            gender = cohort if cohort in {"M", "F"} else "U"
            section_id = match.get("term_section_id")
            key = f"term-section:{section_id}" if section_id else f"unmapped:{gender}:{status}"
            group_key = (key, gender)
            groups.setdefault(
                group_key,
                {
                    "section": match.get("section", ""),
                    "section_key": key,
                    "term_section_id": section_id,
                    "mapping_status": status,
                    "mapping_source": EXAM_ENROLLMENT_SOURCE if status == "mapped" else "",
                    "academic_year": year,
                    "term": term,
                    "gender": gender,
                    "preferred_room": preferred.get(section_id, ""),
                },
            )
            members[group_key].add(sid)
        result[display] = [
            {
                **groups[key],
                "student_count": len(members[key]),
                "membership_fingerprint": _membership_fingerprint(members[key]),
            }
            for key in sorted(
                groups,
                key=lambda key: (
                    groups[key]["gender"],
                    groups[key]["mapping_status"] != "mapped",
                    groups[key]["section"],
                    key[0],
                ),
            )
        ]
        if operations_sections is not None:
            captured = []
            for group in result[display]:
                group_members = members[(group["section_key"], group["gender"])]
                program_counts = Counter(
                    str((program_by_student or {}).get(sid) or "").strip().upper()
                    for sid in group_members
                )
                names = sorted(instructors.get(group["term_section_id"], set()))
                captured.append(
                    {
                        key: group[key]
                        for key in (
                            "section",
                            "section_key",
                            "term_section_id",
                            "mapping_status",
                            "mapping_source",
                            "academic_year",
                            "term",
                            "gender",
                            "student_count",
                        )
                    }
                    | {
                        "program_counts": dict(sorted(program_counts.items())),
                        "instructors": names,
                        # Student timetable scraping verifies section membership,
                        # but instructor names themselves are imported meeting data.
                        "instructor_source": "recorded_section_meetings" if names else "",
                        "instructor_status": "recorded"
                        if names
                        else ("missing" if group["mapping_status"] == "mapped" else "unavailable"),
                    }
                )
            operations_sections[display] = captured
    return result


def summarize_exam_section_mapping(enrollment: dict, course_meta: dict | None = None) -> dict:
    """Counts are exam enrolments, so one student may occur in several courses."""
    metadata = course_meta or {}
    result = {
        "mapped_sections": 0,
        "mapped_enrollments": 0,
        "missing_enrollments": 0,
        "ambiguous_enrollments": 0,
        "details": [],
    }
    mapped_keys: set[tuple[str, str]] = set()
    for code, groups in enrollment.items():
        for group in groups:
            status = group["mapping_status"]
            result[f"{status}_enrollments"] += int(group["student_count"])
            if status == "mapped":
                mapped_keys.add((code, group["section_key"]))
            else:
                result["details"].append(
                    {
                        "course_code": code,
                        **{
                            key: metadata.get(code, {}).get(key, "")
                            for key in ("course_identity", "course_name", "source_course_code")
                        },
                        "gender": group["gender"],
                        "mapping_status": status,
                        "student_count": group["student_count"],
                        "reason": "multiple_recorded_sections"
                        if status == "ambiguous"
                        else "no_recorded_section",
                    }
                )
    result["mapped_sections"] = len(mapped_keys)
    return result


def exam_section_part(group: dict) -> dict:
    """Capture room demand without changing its teaching-section identity."""
    if "section_key" not in group:
        return {
            "section": group["section"],
            "student_count": group["student_count"],
            "_split_from": group.get("_split_from", ""),
        }
    return {
        key: group.get(key)
        for key in (
            "section",
            "section_key",
            "term_section_id",
            "mapping_status",
            "gender",
            "student_count",
        )
    }


def annotate_exam_room_groups(entries: list[dict]) -> None:
    """Number room parts separately; never parse or append to official names."""
    for entry in entries:
        assignments: dict[str, list[int]] = defaultdict(list)
        for index, room in enumerate(entry.get("rooms", [])):
            for part in room.get("section_parts", []):
                key = part.get("section_key")
                if key and index not in assignments[key]:
                    assignments[key].append(index)
        for index, room in enumerate(entry.get("rooms", [])):
            parts = room.get("section_parts", [])
            numbered = [part for part in parts if part.get("section_key")]
            if not numbered:
                continue
            # A section split by capacity can be repacked into one room; combine
            # its fragments before reporting this room's actual section counts.
            merged: dict[str, dict] = {}
            for part in numbered:
                key = part["section_key"]
                if key in merged:
                    merged[key]["student_count"] += part["student_count"]
                else:
                    merged[key] = dict(part)
            parts = list(merged.values())
            for part in parts:
                indices = assignments[part["section_key"]]
                part["room_group_index"] = indices.index(index) + 1
                part["room_group_count"] = len(indices)
            room["section_parts"] = parts
            room["section"] = " + ".join(dict.fromkeys(p["section"] for p in parts if p["section"]))
            room["mapping_status"] = (
                parts[0]["mapping_status"]
                if len({p["mapping_status"] for p in parts}) == 1
                else "mixed"
            )
            room["room_group"] = ""
            if any(p["room_group_count"] > 1 for p in parts):
                room["room_group"] = "; ".join(
                    (f"{p['section'] or 'Section not recorded'}: " if len(parts) > 1 else "")
                    + f"{p['room_group_index']}/{p['room_group_count']}"
                    for p in parts
                )
