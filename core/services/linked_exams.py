"""Linked exams: courses the committee examines as one exam.

A link is a group of two or more courses of one exam timetable that always sit
the same day and period. They are placed, moved, pinned and sent to OVERFLOW
together, and no tool may split them. Everything else stays per real course:
each keeps its own card, section groups, membership fingerprint, roster and
export rows. Links are saved with the run (``result_json["linked_exams"]``),
keyed by course identity - a display code such as "PHYS103 (2)" is renumbered
whenever the population changes, an identity is not.

The solvers treat a link as one scheduling UNIT. This module collapses their
inputs to units - enrolled sets, the conflict graph, study-plan buckets,
credits, pins, placements - and expands their answers back to real courses.
With no links every helper returns its input itself, not a copy, so each
solver runs exactly as it did before links existed.

Two students' facts are deliberately NOT collapsed away. A student registered
in two courses of one link holds two registrations and writes two papers at
the same time: that is a real clash, reported as ``linked_same_slot``, and no
solver can separate the courses to fix it. Daily exam counts and credit loads
in QA stay per real course.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

#: The error codes a caller can act on; each ``LinkedExamsError`` carries one.
INVALID = "linked_exams_invalid"
TOO_FEW_MEMBERS = "linked_exams_too_few_members"
COURSE_REPEATED = "linked_exams_course_repeated"
COURSE_NOT_SELECTED = "linked_exams_course_not_selected"
PINS_DISAGREE = "linked_exams_pins_disagree"
SPLIT = "linked_exams_split"

#: A unit key is its member codes joined by this; no course code contains it.
UNIT_JOINER = "+"


class LinkedExamsError(ValueError):
    """A linked-exams request that cannot be honoured, and the field that says so."""

    def __init__(self, message: str, *, code: str, field: str) -> None:
        super().__init__(message)
        self.code = code
        self.field = field


def _placement(entry: Mapping[str, Any]) -> tuple[str, ...]:
    """Where an entry sits for the purpose of a link: any OVERFLOW index is one place.

    Pinning an unrelated exam renumbers every OVERFLOW entry on the page, and a
    Check gives an entry without an index the next free one, so members that
    are all in OVERFLOW are together whatever their ``Extra-n``.
    """
    if entry.get("day") == "OVERFLOW":
        return ("OVERFLOW",)
    return (str(entry.get("day", "")), str(entry.get("period", "")))


@dataclass(frozen=True)
class LinkedExams:
    """The links of one timetable, resolved to the display codes of one request.

    ``unit_of`` and ``weights`` hold linked courses and units only: a course
    that is in no link is its own unit, of weight one, and appears nowhere.
    ``canonical`` is the saved form: links and members in code order, each
    member named by identity and by its current display code. ``positions``
    is each unit's place in the list the request sent - what an error's
    ``linked_exams[i]`` counts, as the member-level refusals do. When the links
    come from the saved run, the request order is the saved order.
    """

    unit_of: Mapping[str, str] = field(default_factory=dict)
    members: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    weights: Mapping[str, int] = field(default_factory=dict)
    canonical: tuple[dict[str, Any], ...] = ()
    positions: Mapping[str, int] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return bool(self.members)

    # ── units ────────────────────────────────────────────────────────────

    def unit(self, code: str) -> str:
        """The unit ``code`` is scheduled as: its link, or itself."""
        return self.unit_of.get(code, code)

    def members_of(self, unit: str) -> tuple[str, ...]:
        return self.members.get(unit, (unit,))

    def weight(self, unit: str) -> int:
        return self.weights.get(unit, 1)

    def units(self, codes: Iterable[str]) -> Any:
        """The units ``codes`` are scheduled as, in code order."""
        if not self:
            return codes
        return sorted({self.unit(code) for code in codes})

    def units_of(self, codes: Iterable[str]) -> Any:
        """The units touched by ``codes``: a link is pinned or protected if any member is."""
        if not self:
            return codes
        return {self.unit(code) for code in codes}

    # ── collapse: real courses -> units ──────────────────────────────────

    def enrolled(self, enrolled_sets: dict[str, set[int]]) -> dict[str, set[int]]:
        """Each unit's students: the union of its members'."""
        if not self or not enrolled_sets:
            return enrolled_sets
        out: dict[str, set[int]] = {}
        for code, students in enrolled_sets.items():
            unit = self.unit(code)
            if unit == code:
                out[code] = students
            else:
                out[unit] = out.get(unit, set()) | students
        return out

    def adjacency(self, adj: dict[str, dict[str, int]]) -> dict[str, dict[str, int]]:
        """The conflict graph between units.

        Give it the graph AFTER thin-course relaxation: relaxation judges each
        real course by its own size, so a two-student member never relaxes its
        eighty-student partner. An edge inside a link is dropped - no solver can
        separate linked courses - and stays visible as a ``linked_same_slot``
        clash in QA. Parallel edges add up; every solver reads only whether an
        edge exists.
        """
        if not self:
            return adj
        out: dict[str, dict[str, int]] = {}
        for code, neighbours in adj.items():
            unit = self.unit(code)
            row = out.setdefault(unit, {})
            for mate, shared in neighbours.items():
                other = self.unit(mate)
                if other != unit:
                    row[other] = row.get(other, 0) + shared
        return out

    def buckets(
        self,
        plan_term_buckets: dict[tuple[str, int], set[str]] | None,
        course_buckets: dict[str, list[tuple[str, int]]] | None,
    ) -> tuple[Any, Any]:
        """Study-plan buckets over units.

        Linked courses in one bucket count as ONE exam that day, because they
        are one sitting; QA de-duplicates bucket days by unit the same way.
        """
        if not self:
            return plan_term_buckets, course_buckets
        unit_buckets = {
            key: {self.unit(code) for code in codes}
            for key, codes in (plan_term_buckets or {}).items()
        }
        unit_course_buckets: dict[str, list[tuple[str, int]]] = {}
        for code, keys in (course_buckets or {}).items():
            unit = self.unit(code)
            if unit == code:
                unit_course_buckets[code] = keys
                continue
            row = unit_course_buckets.setdefault(unit, [])
            row.extend(key for key in keys if key not in row)
        return unit_buckets, unit_course_buckets

    def credits(self, credit_map: dict[str, int] | None, *, default: int) -> Any:
        """Each unit's credit for scoring: its heaviest member.

        Only the fallback: where members' credits differ, ``student_credits``
        gives the scheduler what each student actually sits.
        """
        if not self or not credit_map:
            return credit_map
        out = {code: credit for code, credit in credit_map.items() if code not in self.unit_of}
        for unit, members in self.members.items():
            out[unit] = max(credit_map.get(code, default) for code in members)
        return out

    def student_credits(
        self,
        credit_map: dict[str, int] | None,
        enrolled_sets: dict[str, set[int]] | None,
        *,
        default: int,
    ) -> dict[str, dict[int, int]]:
        """For a link whose members' credits differ, what each student sits.

        A student in AI212 (3 credits) sits a 3-credit paper even when AI212 is
        linked with a 4-credit course, so the credit-pair scoring stays exact for
        mixed-credit links. A student in two members is scored on the heavier.
        """
        out: dict[str, dict[int, int]] = {}
        if not self or not credit_map or not enrolled_sets:
            return out
        for unit, members in self.members.items():
            credit = {code: credit_map.get(code, default) for code in members}
            if len(set(credit.values())) < 2:
                continue
            sits: dict[int, int] = {}
            for code in members:
                for student in enrolled_sets.get(code, ()):
                    sits[student] = max(sits.get(student, 0), credit[code])
            out[unit] = sits
        return out

    def pins(self, pinned: list[dict] | None) -> Any:
        """One pin per unit. Members' pins must agree; ``resolve_linked_exams`` says so first."""
        if not self or not pinned:
            return pinned
        out: list[dict] = []
        where: dict[str, tuple[str, str]] = {}
        for pin in pinned:
            unit = self.unit(pin["course_code"])
            place = (pin["day"], pin["period"])
            if unit in where:
                if where[unit] != place:
                    raise LinkedExamsError(
                        f"Linked courses {', '.join(self.members_of(unit))} are pinned "
                        "to different times.",
                        code=PINS_DISAGREE,
                        field="pinned",
                    )
                continue
            where[unit] = place
            out.append({**pin, "course_code": unit})
        return out

    def placements(self, placements: dict[str, int]) -> dict[str, int]:
        """Each unit's slot, from placements where every member is present and agrees."""
        if not self:
            return placements
        out: dict[str, int] = {}
        for code, slot in placements.items():
            unit = self.unit(code)
            if out.setdefault(unit, slot) != slot:
                raise self._split_error(unit)
        for unit, members in self.members.items():
            present = [code in placements for code in members]
            if any(present) and not all(present):
                raise self._split_error(unit)
        return out

    def preferred(
        self, preferred_slots: dict[str, int] | None, enrolled_sets: dict[str, set[int]]
    ) -> Any:
        """A unit's preferred slot: its largest member's (the lowest code on a tie)."""
        if not self or not preferred_slots:
            return preferred_slots
        out = {code: slot for code, slot in preferred_slots.items() if code not in self.unit_of}
        for unit, members in self.members.items():
            ranked = sorted(members, key=lambda code: (-len(enrolled_sets.get(code, ())), code))
            leader = next((code for code in ranked if code in preferred_slots), None)
            if leader is not None:
                out[unit] = preferred_slots[leader]
        return out

    # ── expand: units -> real courses ────────────────────────────────────

    def expand_entries(self, entries: list[dict]) -> list[dict]:
        """One entry per member, all at their unit's slot - one ``Extra-n`` for a unit in OVERFLOW."""
        if not self:
            return entries
        out = [
            {**entry, "course_code": code}
            for entry in entries
            for code in self.members_of(entry["course_code"])
        ]
        return sorted(out, key=lambda entry: (entry["slot_index"], entry["course_code"]))

    def expand_codes(self, codes: Iterable[str]) -> Any:
        """The real courses behind ``codes``, in code order."""
        if not self:
            return codes
        return sorted(code for unit in codes for code in self.members_of(unit))

    # ── the one rule ─────────────────────────────────────────────────────

    def split_units(self, entries: Iterable[Mapping[str, Any]]) -> list[str]:
        """Units whose members do not sit together, in code order.

        Together means one real day and period, or every member in OVERFLOW at
        any index. A member missing from ``entries`` is not together either.
        """
        if not self:
            return []
        where = {str(entry.get("course_code", "")): _placement(entry) for entry in entries}
        return [
            unit
            for unit, members in sorted(self.members.items())
            if any(code not in where for code in members)
            or len({where[code] for code in members}) != 1
        ]

    def together(self, entries: Iterable[Mapping[str, Any]]) -> bool:
        return not self.split_units(entries)

    def require_together(self, entries: Iterable[Mapping[str, Any]]) -> None:
        split = self.split_units(entries)
        if split:
            # The first split link in the order the request sent them.
            raise self._split_error(min(split, key=lambda unit: self.positions.get(unit, 0)))

    def _split_error(self, unit: str) -> LinkedExamsError:
        members = self.members_of(unit)
        index = self.positions.get(unit)
        return LinkedExamsError(
            f"Linked courses {', '.join(members)} must sit at the same day and period. "
            "Move them together, then check again.",
            code=SPLIT,
            field="linked_exams" if index is None else f"linked_exams[{index}]",
        )

    def saved(self) -> list[dict[str, Any]]:
        """The links as a run saves them, and as a request may send them back."""
        return [
            {"members": [dict(member) for member in link["members"]]} for link in self.canonical
        ]


NO_LINKS = LinkedExams()


def _identity_of(code: str, meta: Mapping[str, Any]) -> str:
    return str(meta.get("course_identity") or meta.get("source_course_code") or code)


def require_link_list(raw: Any) -> list:
    """What a request sent as ``linked_exams``: a list, or the refusal naming the field.

    For the HTTP boundary, where a missing key and an explicit ``null`` differ:
    a missing key means "the loaded run's links", ``[]`` means "no links", and
    anything else - ``null`` included - is refused rather than read as either.
    """
    if not isinstance(raw, list):
        raise LinkedExamsError(
            "Linked exams must be a list of links.", code=INVALID, field="linked_exams"
        )
    return raw


def resolve_linked_exams(
    raw: Any,
    course_meta: Mapping[str, Mapping[str, Any]],
    *,
    pinned: list[dict] | None = None,
    schedule_entries: Iterable[Mapping[str, Any]] | None = None,
) -> LinkedExams:
    """Validate a request's links against the courses it selected.

    ``course_meta`` maps each selected display code to its metadata (or
    schedule entry); a member names its course by ``course_identity``, which is
    the authority - any ``course_code`` it also sends is replaced by the current
    display code. ``pinned`` must already be validated pins: linked members
    pinned to different times are refused. With ``schedule_entries`` the board
    itself is checked too: a link whose members sit apart is refused, never
    moved on the registrar's behalf.

    Every refusal is a ``LinkedExamsError`` naming the offending field. Nothing
    is ever dropped silently.
    """
    # None is the Python default only; a request's ``null`` never reaches here.
    raw = require_link_list([] if raw is None else raw)
    if not raw:
        return NO_LINKS
    code_by_identity: dict[str, str] = {}
    for code, meta in course_meta.items():
        code_by_identity.setdefault(_identity_of(code, meta), code)
    identity_by_code = {code: identity for identity, code in code_by_identity.items()}

    groups: list[tuple[str, ...]] = []
    seen: set[str] = set()
    for number, link in enumerate(raw):
        where = f"linked_exams[{number}]"
        if not isinstance(link, dict) or not isinstance(link.get("members"), list):
            raise LinkedExamsError(
                "Each linked exam must list its member courses.",
                code=INVALID,
                field=f"{where}.members",
            )
        members = link["members"]
        if len(members) < 2:
            raise LinkedExamsError(
                "A linked exam needs at least two courses.",
                code=TOO_FEW_MEMBERS,
                field=f"{where}.members",
            )
        codes: list[str] = []
        for position, member in enumerate(members):
            member_field = f"{where}.members[{position}].course_identity"
            identity = member.get("course_identity") if isinstance(member, dict) else None
            if not isinstance(identity, str) or not identity.strip():
                raise LinkedExamsError(
                    "Each linked course must be named by its course identity.",
                    code=INVALID,
                    field=member_field,
                )
            code = code_by_identity.get(identity.strip())
            if code is None:
                shown = member.get("course_code") if isinstance(member, dict) else None
                raise LinkedExamsError(
                    f"Linked course {shown if isinstance(shown, str) and shown else identity} "
                    "is not selected for this timetable.",
                    code=COURSE_NOT_SELECTED,
                    field=member_field,
                )
            if code in seen:
                raise LinkedExamsError(
                    f"Course {code} is in more than one linked exam."
                    if code not in codes
                    else f"Course {code} is listed twice in one linked exam.",
                    code=COURSE_REPEATED,
                    field=member_field,
                )
            seen.add(code)
            codes.append(code)
        groups.append(tuple(sorted(codes)))

    unit_of: dict[str, str] = {}
    members_by_unit: dict[str, tuple[str, ...]] = {}
    positions: dict[str, int] = {}
    for number, group in enumerate(groups):
        unit = UNIT_JOINER.join(group)
        if unit in course_meta:
            # Only possible if a course code itself contains the joiner.
            raise LinkedExamsError(
                f"Courses {', '.join(group)} cannot be linked.",
                code=INVALID,
                field=f"linked_exams[{number}]",
            )
        positions[unit] = number
    for group in sorted(groups):
        unit = UNIT_JOINER.join(group)
        members_by_unit[unit] = group
        unit_of.update(dict.fromkeys(group, unit))
    links = LinkedExams(
        unit_of=unit_of,
        members=members_by_unit,
        weights={unit: len(group) for unit, group in members_by_unit.items()},
        positions=positions,
        canonical=tuple(
            {
                "members": [
                    {"course_identity": identity_by_code[code], "course_code": code}
                    for code in group
                ]
            }
            for group in sorted(groups)
        ),
    )
    for number, group in enumerate(groups):
        codes = list(group)
        times = {(pin["day"], pin["period"]) for pin in pinned or [] if pin["course_code"] in codes}
        if len(times) > 1:
            raise LinkedExamsError(
                f"Linked courses {', '.join(codes)} are pinned to different times. "
                "Pin them to one time, or pin one of them.",
                code=PINS_DISAGREE,
                field=f"linked_exams[{number}]",
            )
    if schedule_entries is not None:
        links.require_together(schedule_entries)
    return links


def link_index(result: Mapping[str, Any]) -> dict[str, tuple[str, ...]]:
    """Each linked course of a saved run, by its code in that run, to its partners.

    Members are matched to the run's own schedule by identity, so a reader
    never trusts a stale display code.
    """
    code_by_identity = {
        _identity_of(str(entry.get("course_code", "")), entry): str(entry.get("course_code", ""))
        for entry in result.get("schedule") or []
        if isinstance(entry, dict)
    }
    index: dict[str, tuple[str, ...]] = {}
    for link in result.get("linked_exams") or []:
        members = link.get("members") if isinstance(link, dict) else None
        codes = sorted(
            code
            for member in members or []
            if isinstance(member, dict)
            and (code := code_by_identity.get(str(member.get("course_identity") or "")))
        )
        for code in codes:
            index[code] = tuple(other for other in codes if other != code)
    return {code: partners for code, partners in index.items() if partners}


def linked_exams_qa(
    links: LinkedExams,
    enrolled_sets: Mapping[str, set[int]],
    credit_map: Mapping[str, int],
    course_meta: Mapping[str, Mapping[str, Any]],
) -> dict[str, int]:
    """What the committee is warned about, as counts only - never a student id.

    None of these blocks a build or a save: students registered in two linked
    courses (each one a real clash), links whose members carry different
    credits, and linked courses taught online.
    """
    students_in_two = 0
    for members in links.members.values():
        seen: set[int] = set()
        twice: set[int] = set()
        for code in members:
            students = enrolled_sets.get(code, set())
            twice |= seen & students
            seen |= students
        students_in_two += len(twice)
    return {
        "links": len(links.members),
        "courses": len(links.unit_of),
        "students_in_two_linked_courses": students_in_two,
        "mixed_credit_links": sum(
            len({credit_map.get(code) for code in members}) > 1
            for members in links.members.values()
        ),
        "online_courses": sum(
            bool((course_meta.get(code) or {}).get("is_online")) for code in links.unit_of
        ),
    }
