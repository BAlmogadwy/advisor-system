from __future__ import annotations

import hashlib
import itertools
from collections.abc import Iterable

from core.services.timetable_assignment_models import CanonicalPattern, SectionMeeting
from core.services.timetable_autoplace import _generate_meeting_options
from core.services.timetable_workspace import _to_minutes

DAY_TO_IDX = {"SUN": 0, "MON": 1, "TUE": 2, "WED": 3, "THU": 4}


def get_unique_permutations(
    durations: list[int], allow_permutations: bool
) -> list[tuple[int, ...]]:
    if not allow_permutations:
        return [tuple(durations)]
    return sorted(set(itertools.permutations(durations)))


def generate_pattern_signature(meetings: list[SectionMeeting]) -> str:
    sorted_m = sorted(meetings, key=lambda m: (m.day, m.start_min, m.end_min))
    return "|".join(f"{m.day}-{m.start_min}-{m.end_min}" for m in sorted_m)


def generate_pattern_id(signature: str) -> str:
    return "PAT_" + hashlib.md5(signature.encode(), usedforsecurity=False).hexdigest()[:8]


def duration_family_key(
    durations: Iterable[int], has_lab: bool = False, modality: str = "ONCAMPUS"
) -> str:
    sorted_durations = sorted(durations)
    dur_str = "_".join(map(str, sorted_durations))
    lec_type = "MIXED" if has_lab else "LEC"
    return f"{modality}_{lec_type}_{dur_str}"


def build_canonical_pattern_catalog(
    course_requirements: list[dict],
    slot_config: list[dict] | None = None,
    lab_slot_config: list[dict] | None = None,
    blocked_slots: list[dict] | None = None,
) -> dict[str, list[CanonicalPattern]]:
    catalog: dict[str, list[CanonicalPattern]] = {}
    for req in course_requirements:
        family_key = duration_family_key(
            req["durations"], req.get("has_lab", False), req.get("modality", "ONCAMPUS")
        )
        if family_key in catalog:
            continue
        seen_signatures: dict[str, CanonicalPattern] = {}
        unique_perms = get_unique_permutations(
            req["durations"], req.get("allow_permutations", False)
        )
        for perm in unique_perms:
            perm_str = "_".join(map(str, perm))
            valid_meeting_options = _generate_meeting_options(
                pattern=list(perm),
                slot_config=slot_config or [],
                lab_slot_config=lab_slot_config or [],
                blocked_slots=blocked_slots or [],
            )
            for option_meetings in valid_meeting_options:
                sec_meetings = [
                    SectionMeeting(
                        day=DAY_TO_IDX[m["day"]],
                        start_min=_to_minutes(m["start"]),
                        end_min=_to_minutes(m["end"]),
                    )
                    for m in option_meetings
                ]
                sig = generate_pattern_signature(sec_meetings)
                if sig in seen_signatures:
                    continue
                pat_id = generate_pattern_id(sig)
                days_used = frozenset(m.day for m in sec_meetings)
                slot_fp = "_".join(
                    str(m.start_min // max(1, m.slot_size))
                    for m in sorted(sec_meetings, key=lambda x: (x.day, x.start_min))
                )
                seen_signatures[sig] = CanonicalPattern(
                    pattern_id=pat_id,
                    signature=sig,
                    meetings=sec_meetings,
                    pattern_family=family_key,
                    duration_permutation=perm_str,
                    is_lab_mixed=req.get("has_lab", False),
                    meeting_count=len(sec_meetings),
                    days_used=days_used,
                    slot_fingerprint=slot_fp,
                )
        catalog[family_key] = list(seen_signatures.values())
    return catalog


def get_meeting_pattern_variants(credit_hours: int) -> list[list[int]]:
    if credit_hours == 4:
        return [[100, 75, 75], [75, 100, 75], [75, 75, 100]]
    from core.services.timetable_autoplace import get_meeting_pattern

    return [list(get_meeting_pattern(credit_hours))]
