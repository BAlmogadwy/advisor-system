from __future__ import annotations

import csv
import json
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import fitz

BASE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

django.setup()

from core.models import (  # noqa: E402
    ExamTimetableRun,
    ProgrammeRequirement,
    Room,
    StudentCourse,
)
from core.services.course_identity import planner_course_key  # noqa: E402
from core.services.exam_run_schema import (  # noqa: E402
    STATUS_DERIVATION_VERSION,
    compute_enrolment_snapshot,
    derive_building_footprint,
    derive_multi_sitting_details,
    derive_status_surface,
    load_normalised_run,
    stamp_schema_version,
)
from core.services.exam_timetable import (  # noqa: E402
    _build_qa,
    _build_room_qa,
    assign_rooms_to_schedule,
    build_conflict_graph,
    build_credit_map,
    build_plan_term_buckets,
    check_room_feasibility,
    export_exam_timetable_xlsx,
)

RUNTIME_DIR = BASE_DIR / "runtime"
LABEL = "Imported PDF - Final Exams 1447 Term 2"
RUN_ID = 305
MAX_PER_DAY = 2

PERIODS = [
    ("08:30-10:30", "period 1 (08:30-10:30)"),
    ("11:00-13:00", "period 2 (11:00-13:00)"),
    ("13:30-15:30", "period 3 (13:30-15:30)"),
    ("16:00-18:00", "period 4 (16:00-18:00)"),
]

DAYS = [
    {"day": "Tue 2026-06-02", "band": (104, 220)},
    {"day": "Wed 2026-06-03", "band": (234, 306)},
    {"day": "Thu 2026-06-04", "band": (320, 414)},
    {"day": "Sun 2026-06-07", "band": (428, 512)},
    {"day": "Mon 2026-06-08", "band": (525, 586)},
    {"day": "Tue 2026-06-09", "band": (590, 684)},
    {"day": "Wed 2026-06-10", "band": (698, 731)},
    {"day": "Thu 2026-06-11", "band": (744, 784)},
]

CODE_RE = re.compile(
    r"(?:MS(?:IS|CS|BDA|IOT)\d{3}|ENGL\d{3}|EDCT\d{3}|EDIS\d{3}|EDPA\d{3}|"
    r"STAT\d{3}|MATH\d{3}|PHYS\d{3}|CHEM\d{3}|BIOL\d{3}|ENV\d{3}|ENG\d{3}|"
    r"MGT\d{3}|COE\d{3}|CYB\d{3}|DS\d{3}|AI\d{3}|CS\d{3}|IS\d{3}|EE\d{3}|IE\d{3})"
)


def find_pdf() -> Path:
    downloads = Path.home() / "Downloads"
    exact = [p for p in downloads.glob("*.pdf") if p.stat().st_size == 65918]
    if exact:
        return sorted(exact, key=lambda p: p.stat().st_mtime, reverse=True)[0]
    matches = [
        p
        for p in downloads.glob("*.pdf")
        if p.name.startswith("جدول الاختبارات النهائية للفصل الثاني")
    ]
    if not matches:
        raise FileNotFoundError("Could not find the 1447 final exams PDF in Downloads")
    return sorted(matches, key=lambda p: p.stat().st_mtime, reverse=True)[0]


def day_index_for_y(y: float) -> int | None:
    for i, day in enumerate(DAYS):
        y0, y1 = day["band"]
        if y0 <= y <= y1:
            return i
    return None


def period_index_for_x(x: float) -> int:
    if x >= 520:
        return 0
    if x >= 360:
        return 1
    if x >= 220:
        return 2
    return 3


def code_center(
    span_text: str, code: str, match_start: int, match_end: int, x0: float, x1: float
) -> float:
    code_w = max(14.0, min(30.0, len(code) * 3.4))
    stripped = span_text.strip()
    if stripped == code:
        return (x0 + x1) / 2
    if stripped.startswith(code):
        return x1 - code_w / 2
    if stripped.endswith(code):
        return x0 + code_w / 2

    width = max(1.0, x1 - x0)
    if len(span_text) <= len(code):
        return (x0 + x1) / 2
    # RTL table cells put the first logical token on the visual right.
    ratio = (match_start + (match_end - match_start) / 2) / max(1, len(span_text))
    return x1 - ratio * width


def extract_entries(pdf_path: Path) -> list[dict[str, Any]]:
    doc = fitz.open(pdf_path)
    page = doc[0]
    entries: list[dict[str, Any]] = []
    seen: set[tuple[str, int, int, str, float]] = set()

    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                text = span.get("text", "")
                if not text:
                    continue
                x0, y0, x1, y1 = span["bbox"]
                y = (y0 + y1) / 2
                day_idx = day_index_for_y(y)
                if day_idx is None:
                    continue
                for match in CODE_RE.finditer(text):
                    code = match.group(0)
                    cx = code_center(text, code, match.start(), match.end(), x0, x1)
                    period_idx = period_index_for_x(cx)
                    key = (code, day_idx, period_idx, text, round(y, 1))
                    if key in seen:
                        continue
                    seen.add(key)
                    period, period_label = PERIODS[period_idx]
                    day = DAYS[day_idx]["day"]
                    slot_index = day_idx * len(PERIODS) + period_idx
                    entries.append(
                        {
                            "source_course_code": code,
                            "course_code": code,
                            "day_index": day_idx,
                            "day": day,
                            "period_index": period_idx,
                            "period": period,
                            "period_label": period_label,
                            "slot_index": slot_index,
                            "source_y": round(y, 1),
                            "source_x": round(cx, 1),
                            "source_line": text.strip(),
                        }
                    )

    entries.sort(key=lambda e: (e["day_index"], e["period_index"], e["source_y"], e["source_x"]))
    return entries


def uniquify_duplicate_courses(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts = Counter(e["source_course_code"] for e in entries)
    seen = defaultdict(int)
    out: list[dict[str, Any]] = []
    for entry in entries:
        e = dict(entry)
        source = e["source_course_code"]
        if counts[source] > 1:
            seen[source] += 1
            e["course_code"] = f"{source} ({seen[source]})"
        out.append(e)
    return out


def apply_pdf_verified_corrections(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # Some RTL text spans stretch across period columns in the PDF text layer,
    # so these entries are pinned to the period visible in the rendered table.
    overrides = {
        ("MSIS824", "Tue 2026-06-02"): 2,
        ("COE482", "Tue 2026-06-09"): 2,
    }
    out: list[dict[str, Any]] = []
    for entry in entries:
        e = dict(entry)
        override = overrides.get((e["source_course_code"], e["day"]))
        if override is not None:
            period, period_label = PERIODS[override]
            e["period_index"] = override
            e["period"] = period
            e["period_label"] = period_label
            e["slot_index"] = e["day_index"] * len(PERIODS) + override
            e["source_line"] = f"{e.get('source_line', '')} [PDF verified period override]".strip()
        out.append(e)
    out.sort(key=lambda e: (e["day_index"], e["period_index"], e["source_y"], e["source_x"]))
    return out


def attach_course_identity(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    codes = {e["source_course_code"] for e in entries}
    rows = ProgrammeRequirement.objects.filter(course_code__in=codes).values(
        "course_code",
        "course_name",
        "programme_term",
    )
    names_by_code: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(lambda: 999))
    for row in rows:
        code = str(row["course_code"])
        name = str(row.get("course_name") or "").strip()
        if not name:
            continue
        term = int(row.get("programme_term") or 999)
        names_by_code[code][name] = min(names_by_code[code][name], term)

    by_code: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for entry in entries:
        by_code[entry["source_course_code"]].append(entry)

    out: list[dict[str, Any]] = []
    for code, code_entries in by_code.items():
        names = sorted(names_by_code.get(code, {}).items(), key=lambda item: (item[1], item[0]))
        ordered_entries = sorted(
            code_entries,
            key=lambda e: (e["day_index"], e["period_index"], e["source_y"], e["source_x"]),
        )
        if len(ordered_entries) > 1 and len(names) >= len(ordered_entries):
            assigned_names = [name for name, _term in names[: len(ordered_entries)]]
        elif len(names) == 1:
            assigned_names = [names[0][0]] * len(ordered_entries)
        else:
            assigned_names = [""] * len(ordered_entries)

        for entry, name in zip(ordered_entries, assigned_names, strict=False):
            e = dict(entry)
            e["course_name"] = name
            e["course_identity"] = planner_course_key(code, name)
            out.append(e)

    out.sort(key=lambda e: (e["day_index"], e["period_index"], e["source_y"], e["source_x"]))
    return out


def slots() -> list[dict[str, Any]]:
    out = []
    for day_idx, day in enumerate(DAYS):
        for period_idx, (period, period_label) in enumerate(PERIODS):
            out.append(
                {
                    "index": day_idx * len(PERIODS) + period_idx,
                    "day": day["day"],
                    "period": period,
                    "period_label": period_label,
                }
            )
    return out


def source_enrolled_sets(source_codes: set[str]) -> dict[str, set[int]]:
    enrolled: dict[str, set[int]] = {code: set() for code in source_codes}
    qs = StudentCourse.objects.filter(
        course__course_code__in=source_codes,
        status="studying",
    ).values_list("course__course_code", "student_id")
    for code, student_id in qs.iterator():
        enrolled[str(code)].add(int(student_id))
    return enrolled


def student_course_identity_map(source_codes: set[str]) -> dict[tuple[str, int], str]:
    pr_name_by_program_code = {
        (str(program), str(code)): str(name or "").strip()
        for program, code, name in ProgrammeRequirement.objects.filter(
            course_code__in=source_codes
        ).values_list("program", "course_code", "course_name")
    }
    out: dict[tuple[str, int], str] = {}
    rows = StudentCourse.objects.filter(
        course__course_code__in=source_codes,
        status="studying",
    ).select_related("course", "student")
    for sc in rows:
        source = str(sc.course.course_code)
        sid = int(sc.student_id)
        program = str(sc.student.program or "")
        name = pr_name_by_program_code.get((program, source)) or str(sc.course.description or "")
        out[(source, sid)] = planner_course_key(source, name)
    return out


def split_students_for_display(
    entries: list[dict[str, Any]],
    source_sets: dict[str, set[int]],
) -> dict[str, set[int]]:
    source_to_display: dict[str, list[dict[str, str]]] = defaultdict(list)
    for e in entries:
        source_to_display[e["source_course_code"]].append(
            {
                "course_code": e["course_code"],
                "course_identity": e.get("course_identity") or e["source_course_code"],
            }
        )

    identity_by_student = student_course_identity_map(set(source_to_display))

    display: dict[str, set[int]] = {}
    for source, displays in source_to_display.items():
        sids = set(source_sets.get(source, set()))
        if len(displays) == 1:
            display[displays[0]["course_code"]] = sids
            continue

        by_identity = {d["course_identity"]: d["course_code"] for d in displays}
        buckets: dict[str, set[int]] = {d["course_code"]: set() for d in displays}
        unmatched: list[int] = []
        for sid in sorted(sids):
            identity = identity_by_student.get((source, sid))
            display_code = by_identity.get(identity or "")
            if display_code:
                buckets[display_code].add(sid)
            else:
                unmatched.append(sid)
        ordered = sorted(d["course_code"] for d in displays)
        for idx, sid in enumerate(unmatched):
            buckets[ordered[idx % len(ordered)]].add(sid)
        for display_code, bucket in buckets.items():
            display[display_code] = bucket
    return display


def build_gender_section_enrollment(
    entries: list[dict[str, Any]],
    source_sets: dict[str, set[int]],
) -> dict[str, list[dict[str, Any]]]:
    student_sections: dict[int, str] = dict(
        StudentCourse.objects.filter(
            student_id__in={sid for sids in source_sets.values() for sid in sids}
        )
        .values_list("student_id", "student__section")
        .distinct()
    )

    display_sets = split_students_for_display(entries, source_sets)
    enrollment: dict[str, list[dict[str, Any]]] = {}
    for display_code, sids in display_sets.items():
        by_gender = {"M": set(), "F": set()}
        for sid in sids:
            gender = str(student_sections.get(sid, "")).upper()[:1]
            if gender not in by_gender:
                gender = "M"
            by_gender[gender].add(sid)
        sections = []
        for gender in ("M", "F"):
            count = len(by_gender[gender])
            if count:
                sections.append(
                    {
                        "section": gender,
                        "student_count": count,
                        "preferred_room": "",
                        "gender": gender,
                    }
                )
        if not sections:
            sections.append(
                {
                    "section": "M",
                    "student_count": 0,
                    "preferred_room": "",
                    "gender": "M",
                }
            )
        enrollment[display_code] = sections
    return enrollment


def translate_buckets(
    entries: list[dict[str, Any]],
    ptb_source: dict[tuple[str, int], set[str]],
) -> dict[tuple[str, int], set[str]]:
    source_to_display: dict[str, list[dict[str, str]]] = defaultdict(list)
    for e in entries:
        source_to_display[e["source_course_code"]].append(
            {
                "course_code": e["course_code"],
                "course_identity": e.get("course_identity") or e["source_course_code"],
            }
        )
    row_identities: dict[tuple[str, int, str], str] = {}
    for row in ProgrammeRequirement.objects.filter(course_code__in=set(source_to_display)).values(
        "program",
        "course_code",
        "course_name",
        "programme_term",
    ):
        row_identities[
            (str(row["program"]), int(row.get("programme_term") or 0), str(row["course_code"]))
        ] = planner_course_key(row["course_code"], row.get("course_name"))

    out: dict[tuple[str, int], set[str]] = {}
    for key, source_codes in ptb_source.items():
        display_codes: set[str] = set()
        program, term = key
        for source in source_codes:
            row_identity = row_identities.get((str(program), int(term), source))
            candidates = source_to_display.get(source, [])
            if row_identity:
                matched = [
                    c["course_code"] for c in candidates if c["course_identity"] == row_identity
                ]
                if matched:
                    display_codes.update(matched)
                elif all(c["course_identity"] == source for c in candidates):
                    display_codes.update(c["course_code"] for c in candidates)
            else:
                display_codes.update(c["course_code"] for c in candidates)
        if display_codes:
            out[key] = display_codes
    return out


def translate_credit_map(
    entries: list[dict[str, Any]], source_credit_map: dict[str, int]
) -> dict[str, int]:
    return {
        e["course_code"]: int(source_credit_map.get(e["source_course_code"], 3)) for e in entries
    }


def write_csv(entries: list[dict[str, Any]]) -> None:
    path = RUNTIME_DIR / "imported_exam_timetable_1447_term2_schedule.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "source_course_code",
                "course_code",
                "day",
                "period",
                "slot_index",
                "source_x",
                "source_y",
                "source_line",
            ],
        )
        writer.writeheader()
        for e in entries:
            writer.writerow({k: e.get(k, "") for k in writer.fieldnames})


def main() -> None:
    RUNTIME_DIR.mkdir(exist_ok=True)
    pdf_path = find_pdf()
    extracted = attach_course_identity(
        apply_pdf_verified_corrections(uniquify_duplicate_courses(extract_entries(pdf_path)))
    )
    source_codes = {e["source_course_code"] for e in extracted}
    source_sets = source_enrolled_sets(source_codes)
    display_sets = split_students_for_display(extracted, source_sets)
    display_courses = [e["course_code"] for e in extracted]

    source_credit_map = build_credit_map(source_codes)
    credit_map = translate_credit_map(extracted, source_credit_map)
    conflicts, _adj = build_conflict_graph(display_sets)
    ptb_source, _cb_source = build_plan_term_buckets(source_codes)
    ptb = translate_buckets(extracted, ptb_source)

    schedule_entries = [dict(e) for e in extracted]
    section_enrollment = build_gender_section_enrollment(extracted, source_sets)

    rooms_list = list(
        Room.objects.all().values(
            "room_code",
            "capacity",
            "section",
            "department",
            "building",
            "floor",
            "room_type",
        )
    )
    room_feasibility = check_room_feasibility(section_enrollment, rooms_list)
    assign_rooms_to_schedule(schedule_entries, section_enrollment, rooms_list, seed=None)

    room_meta_by_code = {str(r.get("room_code", "")): r for r in rooms_list}
    for entry in schedule_entries:
        for assignment in entry.get("rooms") or []:
            meta = room_meta_by_code.get(str(assignment.get("room_code", "")))
            if meta:
                assignment.setdefault("building", str(meta.get("building", "") or ""))
                assignment.setdefault("floor", str(meta.get("floor", "") or ""))

    qa = _build_qa(
        display_sets,
        schedule_entries,
        max_per_day=MAX_PER_DAY,
        plan_term_buckets=ptb,
        credit_map=credit_map,
    )
    room_qa = _build_room_qa(schedule_entries, rooms_list)
    qa["rooms"] = room_qa
    qa["room_feasibility_violations"] = room_feasibility
    qa["unassigned_room_sections"] = len(room_qa.get("unassigned_room_sections", []))
    qa["rebalance_moves"] = 0
    qa["thin_threshold"] = 0
    qa["thin_courses"] = []
    qa["thin_clash_risk"] = []
    qa["multi_sitting_details"] = derive_multi_sitting_details(schedule_entries)
    qa["multi_sitting_sections"] = len(qa["multi_sitting_details"])
    qa["manual_override_count"] = qa.get("conflict_count", 0)
    qa["manual_override_details"] = list(qa.get("same_slot_conflicts", []))
    qa["building_footprint"] = derive_building_footprint(schedule_entries)
    qa["enrolment_snapshot"] = compute_enrolment_snapshot(
        display_sets,
        sections_count=sum(len(v) for v in section_enrollment.values()),
        fallback_used=True,
        synthetic_all_sections_count=0,
    )

    buckets_summary = [
        {
            "program": program,
            "programme_term": term,
            "course_count": len(courses),
            "courses": sorted(courses),
        }
        for (program, term), courses in sorted(ptb.items())
    ]
    all_students = set().union(*display_sets.values()) if display_sets else set()
    payload: dict[str, Any] = {
        "status": "ok",
        "students_count": len(all_students),
        "courses": display_courses,
        "courses_count": len(display_courses),
        "conflicts": conflicts,
        "conflicts_count": len(conflicts),
        "slots": slots(),
        "schedule": schedule_entries,
        "qa": qa,
        "buckets_summary": buckets_summary,
        "bucket_count": len(ptb),
        "credit_map": credit_map,
        "seed": None,
        "section_enrollment": section_enrollment,
        "rooms_count": len(rooms_list),
        "assign_rooms": True,
        "source_import": {
            "kind": "pdf",
            "path": str(pdf_path),
            "extraction": "per-code-position-v2",
            "periods_per_day": 4,
            "matched_source_courses": sum(1 for c in source_codes if source_sets.get(c)),
            "source_courses_count": len(source_codes),
        },
    }
    primary_status, status_flags = derive_status_surface(payload)
    payload["primary_status"] = primary_status
    payload["status_flags"] = status_flags
    payload["status_derivation_version"] = STATUS_DERIVATION_VERSION
    stamp_schema_version(payload)

    raw_path = RUNTIME_DIR / "imported_exam_timetable_1447_term2.json"
    enriched_path = RUNTIME_DIR / "imported_exam_timetable_1447_term2_enriched.json"
    raw_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    enriched_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    write_csv(schedule_entries)

    run, _created = ExamTimetableRun.objects.update_or_create(
        id=RUN_ID,
        defaults={
            "label": LABEL,
            "result_json": json.dumps(payload, ensure_ascii=False),
        },
    )
    loaded = load_normalised_run(run)
    xlsx = export_exam_timetable_xlsx(run.id)

    thu = [
        (e["course_code"], e["period"]) for e in loaded["schedule"] if e["day"] == "Thu 2026-06-04"
    ]
    safe_pdf = str(pdf_path).encode("ascii", "backslashreplace").decode("ascii")
    print(f"pdf={safe_pdf}")
    print(f"run_id={run.id}")
    print(
        f"entries={len(schedule_entries)} source_unique={len(source_codes)} display_unique={len(display_courses)}"
    )
    print(
        f"students={loaded['students_count']} rooms={loaded['rooms_count']} conflicts={loaded['conflicts_count']}"
    )
    print(f"primary_status={loaded['primary_status']} flags={','.join(loaded['status_flags'])}")
    print(f"xlsx={xlsx}")
    print("thu_2026_06_04=" + json.dumps(thu, ensure_ascii=False))


if __name__ == "__main__":
    main()
