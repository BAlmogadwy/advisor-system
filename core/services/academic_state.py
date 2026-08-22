"""Canonical, read-only academic facts for one student and one term.

The adviser currently reads the same academic fact through several independent
paths: ``StudentCourse`` for outcomes, ``StudentTermSection`` for timetable
snapshots, ``ProgrammeRequirement`` for programme metadata, and
``ElectiveTermMapping`` for the relationship between a plan placeholder and the
real course a student attends.  None of those tables is wrong, but none can answer
all of the following on its own:

* ``REGISTERED`` and ``EXPECTED`` may coexist and must never be collapsed;
* ``StudentCourse.status='studying'`` is supporting transcript state, not proof of
  a registrar section;
* a timetable can name ``AI463`` while the degree plan and ``StudentCourse`` name
  the fulfilled placeholder ``AI1``;
* course names, credits, type and plan term are programme-scoped whenever a
  ``ProgrammeRequirement`` exists.

This module is the small reconciliation boundary between those authorities.  It
does not mutate or repair any source table, choose sections, or generate prose.
It deliberately preserves the independent dimensions in typed values so a caller
cannot turn an expected plan or a ``studying`` row into registration merely by
reading a convenient combined status.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from core.models import (
    Course,
    ElectiveCourse,
    ElectiveTermMapping,
    ProgrammeRequirement,
    Student,
    StudentCourse,
    TermSection,
)
from core.services.student_helpers import is_elective_slot, normalize_code
from core.services.timetable_snapshots import Snapshot, SnapshotClass, classify_source


class AcademicStateUnavailable(LookupError):
    """The requested student has no authoritative profile to scope the read."""


class EvidenceKind(StrEnum):
    """The claim a timetable link is allowed to support."""

    REGISTERED = "REGISTERED"
    EXPECTED_PLAN = "EXPECTED_PLAN"
    WORKING = "WORKING"


class MetadataSource(StrEnum):
    """Which programme-aware catalogue supplied a course's display metadata."""

    PROGRAMME_REQUIREMENT = "PROGRAMME_REQUIREMENT"
    ELECTIVE_CATALOGUE = "ELECTIVE_CATALOGUE"
    COURSE_CATALOGUE = "COURSE_CATALOGUE"
    TERM_SECTION = "TERM_SECTION"
    UNKNOWN = "UNKNOWN"


class TranscriptStatus(StrEnum):
    """Closed interpretation of ``StudentCourse.status``.

    ``UNKNOWN`` preserves an unfamiliar database value instead of treating it as
    ``NOT_TAKEN``.  A future scraper vocabulary change must not silently change a
    student's standing.
    """

    PASSED = "passed"
    STUDYING = "studying"
    FAILED = "failed"
    NOT_TAKEN = "not_taken"
    UNKNOWN = "unknown"


_KNOWN_TRANSCRIPT_STATUSES = {
    TranscriptStatus.PASSED.value: TranscriptStatus.PASSED,
    TranscriptStatus.STUDYING.value: TranscriptStatus.STUDYING,
    TranscriptStatus.FAILED.value: TranscriptStatus.FAILED,
    TranscriptStatus.NOT_TAKEN.value: TranscriptStatus.NOT_TAKEN,
}


@dataclass(frozen=True)
class MeetingEvidence:
    day: str
    start_time: str
    end_time: str
    room: str
    instructor: str


@dataclass(frozen=True)
class SectionEvidence:
    """One student-to-section link, with repeated meeting rows folded together."""

    kind: EvidenceKind
    term_section_id: int
    course_code: str
    course_name: str
    section: str
    source: str
    registered_count: int | None
    available_capacity: int | None
    meetings: tuple[MeetingEvidence, ...]


@dataclass(frozen=True)
class CourseMetadata:
    """Programme-scoped metadata, with its provenance kept alongside the value."""

    course_code: str
    course_name: str
    credit_hours: int
    programme_term: int | None
    requirement_type: str
    is_elective_placeholder: bool
    source: MetadataSource


@dataclass(frozen=True)
class StudentCourseEvidence:
    """The student's plan/transcript row; never registration proof on its own."""

    status: TranscriptStatus
    raw_status: str
    grade: str
    mark: float | None
    actual_term: str


@dataclass(frozen=True)
class ElectiveOption:
    """A term-scoped, programme-compatible placeholder-to-course mapping."""

    placeholder_code: str
    course_code: str
    course_name: str
    credit_hours: int
    mapping_programme: str


@dataclass(frozen=True)
class CourseAcademicState:
    """Every independent fact known about one normalized course code."""

    course_code: str
    metadata: CourseMetadata
    student_course: StudentCourseEvidence | None
    registered_sections: tuple[SectionEvidence, ...]
    expected_sections: tuple[SectionEvidence, ...]
    working_sections: tuple[SectionEvidence, ...]
    elective_options: tuple[ElectiveOption, ...]
    fulfills_placeholders: tuple[str, ...]
    registered_via_elective_options: tuple[str, ...]
    expected_via_elective_options: tuple[str, ...]
    registered_via_elective_placeholders: tuple[str, ...]
    expected_via_elective_placeholders: tuple[str, ...]

    @property
    def directly_registered(self) -> bool:
        """Whether registrar evidence names this exact code."""
        return bool(self.registered_sections)

    @property
    def registration_confirmed_for_requirement(self) -> bool:
        """Whether this requirement is backed by direct or mapped registrar data.

        For an ordinary course this is identical to :attr:`directly_registered`.
        For an elective placeholder, ``AI463`` registrar evidence can support the
        requirement ``AI1`` only when the exact term mapping says it may.
        """
        return self.directly_registered or bool(self.registered_via_elective_options)

    @property
    def studying_recorded(self) -> bool:
        """Whether ``StudentCourse`` says studying, independently of registration."""
        return bool(self.student_course and self.student_course.status is TranscriptStatus.STUDYING)


@dataclass(frozen=True)
class StudentAcademicState:
    """Immutable academic evidence for one authenticated student and one term."""

    student_id: int
    programme: str
    cohort: str
    academic_year: str
    term: str
    courses: tuple[CourseAcademicState, ...]

    def course(self, course_code: object) -> CourseAcademicState | None:
        """Return one normalized course state without exposing a mutable index."""
        wanted = normalize_code(course_code)
        return next((course for course in self.courses if course.course_code == wanted), None)

    def requirement_course_codes_for(self, course_code: object) -> tuple[str, ...]:
        """Programme requirement identities represented by ``course_code``.

        Ordinary plan courses resolve to themselves. A concrete elective resolves
        to the term-scoped placeholder(s) it may fulfil. Catalogue-only courses
        without a plan relationship return an empty tuple; callers must not invent
        equivalence merely because two codes have similar names.
        """
        course = self.course(course_code)
        if course is None:
            return ()
        requirements = set(course.fulfills_placeholders)
        if course.metadata.source is MetadataSource.PROGRAMME_REQUIREMENT:
            requirements.add(course.course_code)
        return tuple(sorted(requirements))

    @property
    def registered_course_codes(self) -> tuple[str, ...]:
        """Exact codes named by registrar evidence, not inferred placeholders."""
        return tuple(course.course_code for course in self.courses if course.directly_registered)

    @property
    def expected_course_codes(self) -> tuple[str, ...]:
        """Exact codes named by the imported expected plan."""
        return tuple(course.course_code for course in self.courses if course.expected_sections)

    @property
    def studying_course_codes(self) -> tuple[str, ...]:
        """Exact codes marked studying by ``StudentCourse``."""
        return tuple(course.course_code for course in self.courses if course.studying_recorded)

    @property
    def registered_requirement_course_codes(self) -> tuple[str, ...]:
        """Programme requirements whose current study is registrar-supported.

        A real plan course is included when its own code is registered. An
        elective placeholder is also included when a mapped concrete option is
        registered. Concrete options outside the degree-plan table stay out: the
        readiness engine consumes requirement identities, not catalogue aliases.
        """
        return tuple(
            course.course_code
            for course in self.courses
            if course.metadata.source is MetadataSource.PROGRAMME_REQUIREMENT
            and course.registration_confirmed_for_requirement
        )

    @property
    def occupied_elective_slot_codes(self) -> tuple[str, ...]:
        """Plan slots backed by direct or mapped registrar evidence.

        This is the stable requirement identity a recommendation engine needs. If
        the registrar returns ``AI1`` directly, or returns mapped course ``AI463``,
        the occupied degree-plan requirement is ``AI1`` in both cases.
        """
        return tuple(
            course.course_code
            for course in self.courses
            if course.metadata.is_elective_placeholder
            and course.registration_confirmed_for_requirement
        )

    @property
    def expected_elective_slot_codes(self) -> tuple[str, ...]:
        """Plan slots occupied in expected-plan evidence, never registrar fact."""
        return tuple(
            course.course_code
            for course in self.courses
            if course.metadata.is_elective_placeholder
            and (course.expected_sections or course.expected_via_elective_options)
        )

    @property
    def registered_or_equivalent_course_codes(self) -> tuple[str, ...]:
        """Codes a recommender must suppress for already occupied requirements.

        Once an elective slot is occupied, every concrete option mapped to that
        slot is an equivalent recommendation target and must be suppressed.  The
        exact registrar codes remain available separately in
        :attr:`registered_course_codes`; this expanded set is for planning and
        de-duplication, not for wording a registration claim.
        """
        occupied_slots = set(self.occupied_elective_slot_codes)
        codes = set(self.registered_course_codes) | occupied_slots
        for course in self.courses:
            if course.course_code in occupied_slots:
                codes.update(option.course_code for option in course.elective_options)
        return tuple(sorted(codes))

    @property
    def expected_or_equivalent_course_codes(self) -> tuple[str, ...]:
        """Expected-plan analogue of :attr:`registered_or_equivalent_course_codes`."""
        occupied_slots = set(self.expected_elective_slot_codes)
        codes = set(self.expected_course_codes) | occupied_slots
        for course in self.courses:
            if course.course_code in occupied_slots:
                codes.update(option.course_code for option in course.elective_options)
        return tuple(sorted(codes))

    def registered_supporting_course_codes(self, course_code: object) -> tuple[str, ...]:
        """Exact registrar codes supporting suppression of ``course_code``."""
        course = self.course(course_code)
        if course is None:
            return ()
        support = set(course.registered_via_elective_options)
        support.update(course.registered_via_elective_placeholders)
        if course.directly_registered:
            support.add(course.course_code)
        # A sibling concrete option is also suppressed after the shared slot is
        # occupied. Its own row has no direct link to the registered sibling, so
        # resolve through the placeholder and return the exact registrar code(s).
        for placeholder_code in course.fulfills_placeholders:
            placeholder = self.course(placeholder_code)
            if placeholder is None:
                continue
            support.update(placeholder.registered_via_elective_options)
            if placeholder.directly_registered:
                support.add(placeholder.course_code)
        return tuple(sorted(support))

    def expected_supporting_course_codes(self, course_code: object) -> tuple[str, ...]:
        """Exact expected-plan codes supporting suppression of ``course_code``."""
        course = self.course(course_code)
        if course is None:
            return ()
        support = set(course.expected_via_elective_options)
        support.update(course.expected_via_elective_placeholders)
        if course.expected_sections:
            support.add(course.course_code)
        for placeholder_code in course.fulfills_placeholders:
            placeholder = self.course(placeholder_code)
            if placeholder is None:
                continue
            support.update(placeholder.expected_via_elective_options)
            if placeholder.expected_sections:
                support.add(placeholder.course_code)
        return tuple(sorted(support))

    def as_evidence_payload(self) -> dict[str, Any]:
        """A narrow JSON-ready adapter for a future adviser/validator integration.

        The payload repeats the three independent code lists so a consumer does
        not need to infer registration from transcript status or from expected
        sections.  It contains facts only; no Arabic answer text or policy claim.
        """
        return {
            "student_id": self.student_id,
            "programme": self.programme,
            "cohort": self.cohort,
            "academic_year": self.academic_year,
            "term": self.term,
            "registered_course_codes": list(self.registered_course_codes),
            "registered_or_equivalent_course_codes": list(
                self.registered_or_equivalent_course_codes
            ),
            "expected_course_codes": list(self.expected_course_codes),
            "expected_or_equivalent_course_codes": list(self.expected_or_equivalent_course_codes),
            "studying_course_codes": list(self.studying_course_codes),
            "occupied_elective_slot_codes": list(self.occupied_elective_slot_codes),
            "registered_requirement_course_codes": list(self.registered_requirement_course_codes),
            "expected_elective_slot_codes": list(self.expected_elective_slot_codes),
            "courses": [_course_payload(course) for course in self.courses],
        }


def programme_variants(programme: str) -> tuple[str, ...]:
    """Mapping catalogues use the department code for versioned plans (AI2→AI)."""
    normalized = normalize_code(programme)
    variants = [normalized]
    if normalized.endswith("2") and len(normalized) > 1:
        variants.append(normalized[:-1])
    return tuple(variants)


def _evidence_kind(source: object) -> EvidenceKind:
    snapshot_class = classify_source(source)
    if snapshot_class is SnapshotClass.REGISTRAR:
        return EvidenceKind.REGISTERED
    if snapshot_class is SnapshotClass.EXPECTED:
        return EvidenceKind.EXPECTED_PLAN
    return EvidenceKind.WORKING


def _load_sections(
    student_id: int,
    academic_year: str,
    term: str,
) -> tuple[SectionEvidence, ...]:
    """Load the existing visibility-filtered baseline and fold its meeting rows."""
    # Imported at the read boundary so tests and operational instrumentation that
    # wrap the established baseline reader still see this canonical consumer.
    from core.services.student_sections import get_student_term_baseline

    rows = get_student_term_baseline(
        student_id,
        academic_year,
        term,
        snapshot=Snapshot.ANY,
    )
    section_ids: set[int] = set()
    for row in rows:
        section_id = row.get("term_section_id")
        if isinstance(section_id, int):
            section_ids.add(section_id)
    capacities = {
        row["id"]: row["available_capacity"]
        for row in TermSection.objects.filter(id__in=section_ids).values("id", "available_capacity")
    }

    grouped: dict[tuple[EvidenceKind, int, str], dict[str, Any]] = {}
    for row in rows:
        section_id = row.get("term_section_id")
        if not isinstance(section_id, int):
            # The baseline reader currently emits only persisted sections.  Keep
            # this defensive guard so a future synthetic fallback cannot be
            # silently upgraded to section evidence.
            continue
        source = str(row.get("source") or "")
        kind = _evidence_kind(source)
        key = (kind, section_id, source)
        entry = grouped.setdefault(
            key,
            {
                "kind": kind,
                "term_section_id": section_id,
                "course_code": normalize_code(
                    row.get("course_key") or row.get("course_code") or ""
                ),
                "course_name": str(row.get("course_name") or ""),
                "section": str(row.get("section") or ""),
                "source": source,
                "registered_count": row.get("registered_count"),
                "available_capacity": capacities.get(section_id),
                "meetings": set(),
            },
        )
        meeting = MeetingEvidence(
            day=str(row.get("day") or ""),
            start_time=str(row.get("start_time") or ""),
            end_time=str(row.get("end_time") or ""),
            room=str(row.get("room") or ""),
            instructor=str(row.get("instructor") or ""),
        )
        if any(
            (meeting.day, meeting.start_time, meeting.end_time, meeting.room, meeting.instructor)
        ):
            entry["meetings"].add(meeting)

    sections = [
        SectionEvidence(
            kind=entry["kind"],
            term_section_id=entry["term_section_id"],
            course_code=entry["course_code"],
            course_name=entry["course_name"],
            section=entry["section"],
            source=entry["source"],
            registered_count=(
                int(entry["registered_count"])
                if isinstance(entry["registered_count"], int)
                else None
            ),
            available_capacity=(
                int(entry["available_capacity"])
                if isinstance(entry["available_capacity"], int)
                else None
            ),
            meetings=tuple(
                sorted(
                    entry["meetings"],
                    key=lambda meeting: (
                        meeting.day,
                        meeting.start_time,
                        meeting.end_time,
                        meeting.room,
                        meeting.instructor,
                    ),
                )
            ),
        )
        for entry in grouped.values()
        if entry["course_code"]
    ]
    return tuple(
        sorted(
            sections,
            key=lambda section: (
                section.course_code,
                section.section,
                section.kind.value,
                section.source,
                section.term_section_id,
            ),
        )
    )


def _load_elective_options(
    programme: str,
    academic_year: str,
    term: str,
) -> tuple[ElectiveOption, ...]:
    try:
        term_number = int(term)
    except (TypeError, ValueError):
        return ()

    variants = programme_variants(programme)
    allowed_placeholders = {
        normalize_code(row["course_code"]): int(row["credit_hours"] or 0)
        for row in ProgrammeRequirement.objects.filter(program__iexact=programme).values(
            "course_code", "type", "credit_hours"
        )
        if is_elective_slot(row["type"])
    }
    if not allowed_placeholders:
        return ()
    rows = (
        ElectiveTermMapping.objects.filter(
            programme__in=variants,
            academic_year=academic_year,
            term=term_number,
            placeholder_code__in=allowed_placeholders,
        )
        .select_related("elective")
        .order_by("placeholder_code", "elective__course_code")
    )
    # Prefer a mapping written for the exact curriculum version over the base
    # department mapping when both name the same option.  The base mapping remains
    # the intentional fallback for versioned programmes such as AI2.
    ordered_rows = sorted(
        rows,
        key=lambda row: (
            normalize_code(row.placeholder_code),
            normalize_code(row.elective.course_code),
            0 if normalize_code(row.programme) == normalize_code(programme) else 1,
            normalize_code(row.programme),
        ),
    )
    seen: set[tuple[str, str]] = set()
    options: list[ElectiveOption] = []
    for row in ordered_rows:
        placeholder_code = normalize_code(row.placeholder_code)
        course_code = normalize_code(row.elective.course_code)
        key = (placeholder_code, course_code)
        if not placeholder_code or not course_code or key in seen:
            continue
        if normalize_code(row.elective.programme) not in variants:
            # A database FK proves only that the elective exists, not that it
            # belongs to this programme.  Never use a cross-programme mapping as
            # evidence that one course fulfils another plan's requirement.
            continue
        slot_credits = allowed_placeholders.get(placeholder_code, 0)
        option_credits = int(row.elective.credit_hours or 0)
        if slot_credits and option_credits and slot_credits != option_credits:
            continue
        seen.add(key)
        options.append(
            ElectiveOption(
                placeholder_code=placeholder_code,
                course_code=course_code,
                course_name=str(row.elective.course_name or ""),
                credit_hours=int(row.elective.credit_hours or 0),
                mapping_programme=normalize_code(row.programme),
            )
        )
    return tuple(options)


def _metadata_by_code(
    programme: str,
    codes: set[str],
    sections: tuple[SectionEvidence, ...],
    options: tuple[ElectiveOption, ...],
) -> dict[str, CourseMetadata]:
    """Resolve metadata in authority order, scoped to the student's programme."""
    requirements = {
        normalize_code(row["course_code"]): row
        for row in ProgrammeRequirement.objects.filter(program__iexact=programme).values(
            "course_code",
            "course_name",
            "credit_hours",
            "programme_term",
            "type",
        )
        if normalize_code(row["course_code"])
    }
    elective_rows: dict[str, ElectiveCourse] = {}
    # A registrar section can name a catalogue elective that is not mapped to a
    # placeholder in this term (for example a free elective, or a mapping import
    # that has not arrived yet).  Its catalogue name/credits remain authoritative
    # metadata even though no slot-equivalence claim may be made from it.
    elective_candidates = ElectiveCourse.objects.filter(
        programme__in=programme_variants(programme), course_code__in=codes
    )
    for row in sorted(
        elective_candidates,
        key=lambda candidate: (
            normalize_code(candidate.course_code),
            0 if normalize_code(candidate.programme) == normalize_code(programme) else 1,
            normalize_code(candidate.programme),
        ),
    ):
        elective_rows.setdefault(normalize_code(row.course_code), row)
    catalogue = {
        normalize_code(row.course_code): row for row in Course.objects.filter(course_code__in=codes)
    }
    section_names: dict[str, str] = {}
    for section in sections:
        if section.course_name:
            section_names.setdefault(section.course_code, section.course_name)

    out: dict[str, CourseMetadata] = {}
    for code in sorted(codes):
        requirement = requirements.get(code)
        if requirement is not None:
            requirement_type = str(requirement["type"] or "")
            out[code] = CourseMetadata(
                course_code=code,
                course_name=str(requirement["course_name"] or ""),
                credit_hours=int(requirement["credit_hours"] or 0),
                programme_term=(
                    int(requirement["programme_term"])
                    if isinstance(requirement["programme_term"], int)
                    else None
                ),
                requirement_type=requirement_type,
                is_elective_placeholder=is_elective_slot(requirement_type),
                source=MetadataSource.PROGRAMME_REQUIREMENT,
            )
            continue

        elective = elective_rows.get(code)
        if elective is not None:
            out[code] = CourseMetadata(
                course_code=code,
                course_name=str(elective.course_name or ""),
                credit_hours=int(elective.credit_hours or 0),
                programme_term=None,
                requirement_type=str(elective.category or ""),
                is_elective_placeholder=False,
                source=MetadataSource.ELECTIVE_CATALOGUE,
            )
            continue

        course = catalogue.get(code)
        if course is not None:
            out[code] = CourseMetadata(
                course_code=code,
                course_name=str(course.description or ""),
                credit_hours=int(course.credit_hours or 0),
                programme_term=None,
                requirement_type="",
                is_elective_placeholder=False,
                source=MetadataSource.COURSE_CATALOGUE,
            )
            continue

        section_name = section_names.get(code, "")
        out[code] = CourseMetadata(
            course_code=code,
            course_name=section_name,
            credit_hours=0,
            programme_term=None,
            requirement_type="",
            is_elective_placeholder=False,
            source=MetadataSource.TERM_SECTION if section_name else MetadataSource.UNKNOWN,
        )
    return out


def _student_course_evidence(student_id: int) -> dict[str, StudentCourseEvidence]:
    out: dict[str, StudentCourseEvidence] = {}
    rows = StudentCourse.objects.filter(student_id=student_id).select_related("course")
    for row in rows:
        code = normalize_code(row.course.course_code)
        if not code:
            continue
        raw_status = str(row.status or "").strip().casefold()
        out[code] = StudentCourseEvidence(
            status=_KNOWN_TRANSCRIPT_STATUSES.get(raw_status, TranscriptStatus.UNKNOWN),
            raw_status=raw_status,
            grade=str(row.grade or ""),
            mark=float(row.mark) if row.mark is not None else None,
            actual_term=str(row.actual_term or ""),
        )
    return out


def build_student_academic_state(
    student_id: int | str,
    academic_year: str,
    term: str | int,
) -> StudentAcademicState:
    """Return the canonical read-only evidence for one student and one term.

    Registration is supported only by ``REGISTERED`` section evidence.  Expected
    rows stay planning evidence, and ``StudentCourse.status='studying'`` stays a
    separate transcript fact.  A placeholder is considered registration-backed
    only when the exact year/term mapping connects it to a registered concrete
    course.
    """
    try:
        normalized_student_id = int(student_id)
    except (TypeError, ValueError) as exc:
        raise AcademicStateUnavailable(f"Invalid student id: {student_id!r}") from exc

    student = Student.objects.filter(student_id=normalized_student_id).first()
    if student is None:
        raise AcademicStateUnavailable(f"Student {normalized_student_id} was not found")
    programme = normalize_code(student.program)
    if not programme:
        raise AcademicStateUnavailable(
            f"Student {normalized_student_id} has no programme; academic facts cannot be scoped"
        )

    normalized_year = str(academic_year).strip()
    normalized_term = str(term).strip()
    sections = _load_sections(normalized_student_id, normalized_year, normalized_term)
    options = _load_elective_options(programme, normalized_year, normalized_term)
    student_courses = _student_course_evidence(normalized_student_id)

    codes = set(student_courses)
    codes.update(
        normalize_code(code)
        for code in ProgrammeRequirement.objects.filter(program__iexact=programme).values_list(
            "course_code", flat=True
        )
        if normalize_code(code)
    )
    codes.update(section.course_code for section in sections)
    codes.update(option.placeholder_code for option in options)
    codes.update(option.course_code for option in options)
    metadata = _metadata_by_code(programme, codes, sections, options)

    sections_by_code: dict[str, dict[EvidenceKind, list[SectionEvidence]]] = {}
    for section in sections:
        sections_by_code.setdefault(section.course_code, {}).setdefault(section.kind, []).append(
            section
        )
    options_by_placeholder: dict[str, list[ElectiveOption]] = {}
    placeholders_by_option: dict[str, set[str]] = {}
    for option in options:
        options_by_placeholder.setdefault(option.placeholder_code, []).append(option)
        placeholders_by_option.setdefault(option.course_code, set()).add(option.placeholder_code)

    registered_codes = {
        section.course_code for section in sections if section.kind is EvidenceKind.REGISTERED
    }
    expected_codes = {
        section.course_code for section in sections if section.kind is EvidenceKind.EXPECTED_PLAN
    }
    course_states: list[CourseAcademicState] = []
    for code in sorted(codes):
        grouped = sections_by_code.get(code, {})
        course_options = tuple(options_by_placeholder.get(code, ()))
        course_states.append(
            CourseAcademicState(
                course_code=code,
                metadata=metadata[code],
                student_course=student_courses.get(code),
                registered_sections=tuple(grouped.get(EvidenceKind.REGISTERED, ())),
                expected_sections=tuple(grouped.get(EvidenceKind.EXPECTED_PLAN, ())),
                working_sections=tuple(grouped.get(EvidenceKind.WORKING, ())),
                elective_options=course_options,
                fulfills_placeholders=tuple(sorted(placeholders_by_option.get(code, ()))),
                registered_via_elective_options=tuple(
                    sorted(
                        option.course_code
                        for option in course_options
                        if option.course_code in registered_codes
                    )
                ),
                expected_via_elective_options=tuple(
                    sorted(
                        option.course_code
                        for option in course_options
                        if option.course_code in expected_codes
                    )
                ),
                registered_via_elective_placeholders=tuple(
                    sorted(
                        placeholder
                        for placeholder in placeholders_by_option.get(code, ())
                        if placeholder in registered_codes
                    )
                ),
                expected_via_elective_placeholders=tuple(
                    sorted(
                        placeholder
                        for placeholder in placeholders_by_option.get(code, ())
                        if placeholder in expected_codes
                    )
                ),
            )
        )

    cohort = str(student.section or "").strip().upper()
    return StudentAcademicState(
        student_id=normalized_student_id,
        programme=programme,
        cohort=cohort if cohort in {"M", "F"} else "",
        academic_year=normalized_year,
        term=normalized_term,
        courses=tuple(course_states),
    )


def _meeting_payload(meeting: MeetingEvidence) -> dict[str, str]:
    return {
        "day": meeting.day,
        "start_time": meeting.start_time,
        "end_time": meeting.end_time,
        "room": meeting.room,
        "instructor": meeting.instructor,
    }


def _section_payload(section: SectionEvidence) -> dict[str, Any]:
    return {
        "evidence_kind": section.kind.value,
        "term_section_id": section.term_section_id,
        "course_code": section.course_code,
        "course_name": section.course_name,
        "section": section.section,
        "source": section.source,
        "registered_count": section.registered_count,
        "available_capacity": section.available_capacity,
        "meetings": [_meeting_payload(meeting) for meeting in section.meetings],
    }


def _course_payload(course: CourseAcademicState) -> dict[str, Any]:
    student_course = course.student_course
    return {
        "course_code": course.course_code,
        "metadata": {
            "course_name": course.metadata.course_name,
            "credit_hours": course.metadata.credit_hours,
            "programme_term": course.metadata.programme_term,
            "requirement_type": course.metadata.requirement_type,
            "is_elective_placeholder": course.metadata.is_elective_placeholder,
            "source": course.metadata.source.value,
        },
        "student_course": (
            {
                "status": student_course.status.value,
                "raw_status": student_course.raw_status,
                "grade": student_course.grade,
                "mark": student_course.mark,
                "actual_term": student_course.actual_term,
            }
            if student_course
            else None
        ),
        "registered_sections": [_section_payload(row) for row in course.registered_sections],
        "expected_sections": [_section_payload(row) for row in course.expected_sections],
        "working_sections": [_section_payload(row) for row in course.working_sections],
        "elective_options": [
            {
                "placeholder_code": option.placeholder_code,
                "course_code": option.course_code,
                "course_name": option.course_name,
                "credit_hours": option.credit_hours,
                "mapping_programme": option.mapping_programme,
            }
            for option in course.elective_options
        ],
        "fulfills_placeholders": list(course.fulfills_placeholders),
        "registered_via_elective_options": list(course.registered_via_elective_options),
        "expected_via_elective_options": list(course.expected_via_elective_options),
        "registered_via_elective_placeholders": list(course.registered_via_elective_placeholders),
        "expected_via_elective_placeholders": list(course.expected_via_elective_placeholders),
        "directly_registered": course.directly_registered,
        "registration_confirmed_for_requirement": (course.registration_confirmed_for_requirement),
        "studying_recorded": course.studying_recorded,
    }


__all__ = [
    "AcademicStateUnavailable",
    "CourseAcademicState",
    "CourseMetadata",
    "ElectiveOption",
    "EvidenceKind",
    "MeetingEvidence",
    "MetadataSource",
    "SectionEvidence",
    "StudentAcademicState",
    "StudentCourseEvidence",
    "TranscriptStatus",
    "build_student_academic_state",
    "programme_variants",
]
