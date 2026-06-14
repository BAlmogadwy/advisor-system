"""Instructor assignment — the single write path for section ↔ instructor links.

``SectionInstructor`` links are the source of truth; ``TermSectionMeeting.instructor``
is a write-through *display cache* of the primary instructor's name. Writing both
on every assign means the existing free-text-based clash/conflict readers keep
working the moment a section gets an instructor — the structured links then power
the load report and (opt-in) the links-keyed multi-instructor clash.

Used by both the management page and the workspace drawer so the contract lives
in one place.
"""

from __future__ import annotations

from django.db import transaction

from core.models import Instructor, SectionInstructor, TermSection, TermSectionMeeting
from core.services.timetable_pr4_instructor import normalise_instructor


def get_or_create_instructor(name: str) -> Instructor | None:
    """Resolve a free-text name to an ``Instructor`` (creating it if new).

    Dedupe key is the normalised (strip+casefold) name, matching the planner's
    ``normalise_instructor`` discipline. Whitespace-only ⇒ ``None``.
    """
    norm = normalise_instructor(name)
    if not norm:
        return None
    instructor, _created = Instructor.objects.get_or_create(
        normalised_name=norm,
        defaults={"full_name": name.strip()},
    )
    return instructor


def serialize_section_instructors(term_section: TermSection) -> list[dict[str, object]]:
    """The section's instructor links, primary first, JSON-safe."""
    links = (
        SectionInstructor.objects.filter(term_section=term_section)
        .select_related("instructor")
        .order_by("id")  # insertion order ⇒ primary (created first) leads
    )
    return [
        {
            "id": link.instructor_id,
            "full_name": link.instructor.full_name,
            "full_name_ar": link.instructor.full_name_ar,
            "role": link.role,
            "is_active": link.instructor.is_active,
        }
        for link in links
    ]


def _display_name_for(instructors: list[Instructor]) -> str:
    """The string fanned into the meeting rows — the primary instructor's name.

    v1 writes only the primary name (single free-text field) to avoid the
    multi-instructor delimiter problem; the structured links carry the full set.
    """
    return instructors[0].full_name if instructors else ""


@transaction.atomic
def set_section_instructors(
    term_section: TermSection,
    instructor_ids: list[int] | None = None,
    instructor_names: list[str] | None = None,
) -> list[dict[str, object]]:
    """Replace the section's instructor set.

    Accepts ``instructor_ids`` (existing instructors) and/or ``instructor_names``
    (resolved/created via ``get_or_create_instructor``). The first resolved
    instructor is the *primary* (role ``primary``); the rest are ``co``. Passing
    an empty set clears all instructors and reverts the meeting rows to ``""``.

    Returns the serialised instructor links.
    """
    resolved: list[Instructor] = []
    seen: set[int] = set()

    for iid in instructor_ids or []:
        instructor = Instructor.objects.filter(pk=iid).first()
        if instructor is None:
            raise ValueError(f"Instructor {iid} not found")
        if instructor.pk not in seen:
            seen.add(instructor.pk)
            resolved.append(instructor)

    for name in instructor_names or []:
        instructor = get_or_create_instructor(name)
        if instructor is not None and instructor.pk not in seen:
            seen.add(instructor.pk)
            resolved.append(instructor)

    # Replace the link set.
    SectionInstructor.objects.filter(term_section=term_section).delete()
    for idx, instructor in enumerate(resolved):
        SectionInstructor.objects.create(
            term_section=term_section,
            instructor=instructor,
            role="primary" if idx == 0 else "co",
        )

    # Write-through the primary name into the section's meeting rows (display cache).
    TermSectionMeeting.objects.filter(term_section=term_section).update(
        instructor=_display_name_for(resolved)
    )

    return serialize_section_instructors(term_section)
