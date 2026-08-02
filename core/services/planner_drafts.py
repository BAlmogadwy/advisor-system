"""The life of a planner draft: edit, confirm, generate, choose.

A draft is a selection carried across a navigation. Everything dangerous about it
follows from that one sentence — it arrives by reference, it can be edited between
being confirmed and being acted on, and two tabs can act on it at once.

Three rules hold it together.

**Editing is one function.** Every material change invalidates the confirmation and
the stored alternatives, because both describe inputs that no longer exist. Spread
that invalidation across the endpoints and one of them eventually forgets, which
shows up as a student confirming a harmless rebuild, changing what it contains, and
having the old approval carry.

**Confirmation is server state.** A posted `{"confirmed": true}` is written by
whoever holds the keyboard. A token is issued by the server, stored as a hash,
bound to one student, one draft and one version, used once, and dead the moment the
draft changes.

**Generation is idempotent by version.** The draft is locked, and a second request
for a version that already has a result gets that result rather than a second run
of the solver — otherwise two tabs produce two different sets of timetables and the
student is comparing one while looking at the other.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import timedelta
from typing import Any

from django.db import transaction
from django.utils import timezone

from core.models import PlannerDraft
from core.services.student_planner import (
    DraftRejected,
    PlannerRequest,
    build_student_options,
    validate_draft_selection,
)

#: Bumped when the SHAPE of a stored generation changes, so an old row is
#: detectable rather than silently misread.
GENERATION_SCHEMA_VERSION = "1.0"

#: Long enough to read the confirmation and decide; short enough that a token left
#: in a closed tab is not still valid tomorrow.
TOKEN_TTL = timedelta(minutes=15)

#: A draft describes a catalogue. Past this it may describe one that no longer
#: exists, so it dies rather than lingering as a stale suggestion.
DRAFT_TTL = timedelta(hours=24)


class DraftError(Exception):
    """The draft cannot be used this way."""


class DraftExpired(DraftError):
    """Too old to act on."""


class ConfirmationRequired(DraftError):
    """Rebuilding discards the student's own section choices; it must be confirmed."""


def owned_draft(student_id: int, draft_id: Any, *, lock: bool = False) -> PlannerDraft:
    """Fetch a draft this student owns, or 404.

    Ownership is in the filter, as everywhere else in the adviser: fetching by id
    and checking afterwards is one forgotten line away from a leak, and the
    forgotten line looks like working code.
    """
    import uuid as _uuid

    from django.http import Http404

    try:
        parsed = _uuid.UUID(str(draft_id))
    except (ValueError, AttributeError, TypeError):
        raise Http404("No such draft") from None

    rows = PlannerDraft.objects.filter(id=parsed, student_id=int(student_id))
    if lock:
        rows = rows.select_for_update()
    draft = rows.first()
    if draft is None:
        raise Http404("No such draft")
    return draft


def planning_term() -> tuple[str, str]:
    """The term a new draft plans for, from the project's own configured defaults.

    The same source the adviser runtime already defaults to, asked rather than
    re-derived — a second opinion about "which term is it" is a second answer.
    """
    from core.settings_views import load_defaults

    defaults = load_defaults()
    return str(defaults["academic_year"]), str(defaults["term"])


def create_draft(
    *,
    student_id: int,
    course_codes: Any,
    fixed_sections: Any = None,
    keep_current_sections: bool = True,
    source_message: Any = None,
) -> PlannerDraft:
    """Validate a selection and store it. Nothing is trusted from the caller.

    The term is NOT a parameter. It comes from the configured defaults, so no
    caller — chat, screen or otherwise — can plan a student into a term by naming
    one in a payload.
    """
    codes, pinned = validate_draft_selection(int(student_id), course_codes, fixed_sections or {})
    year, term = planning_term()
    return PlannerDraft.objects.create(
        student_id=int(student_id),
        academic_year=year,
        term=term,
        course_codes=codes,
        fixed_sections=pinned,
        keep_current_sections=bool(keep_current_sections),
        source_message=source_message,
        expires_at=timezone.now() + DRAFT_TTL,
    )


def edit_draft(
    draft: PlannerDraft,
    *,
    course_codes: Any = None,
    fixed_sections: Any = None,
    keep_current_sections: bool | None = None,
) -> PlannerDraft:
    """THE only way a draft changes, so invalidation cannot be forgotten.

    A material edit — a course added or removed, a pin set, cleared or moved, or the
    retain choice flipped — makes both the confirmation and the stored alternatives
    describe inputs that no longer exist. Both go.
    """
    _require_live(draft)

    codes = draft.course_codes if course_codes is None else course_codes
    pins = draft.fixed_sections if fixed_sections is None else fixed_sections
    validated_codes, validated_pins = validate_draft_selection(draft.student_id, codes, pins)
    keep = (
        draft.keep_current_sections
        if keep_current_sections is None
        else bool(keep_current_sections)
    )

    unchanged = (
        validated_codes == list(draft.course_codes or [])
        and validated_pins == dict(draft.fixed_sections or {})
        and keep == draft.keep_current_sections
    )
    if unchanged:
        return draft

    draft.course_codes = validated_codes
    draft.fixed_sections = validated_pins
    draft.keep_current_sections = keep
    draft.version += 1
    # Everything downstream of the inputs dies with them.
    draft.rebuild_token_hash = ""
    draft.rebuild_token_version = 0
    draft.rebuild_token_expires_at = None
    draft.alternatives = []
    draft.generated_inputs = {}
    draft.generated_at = None
    draft.generation_schema_version = ""
    draft.selected_alternative = ""
    draft.selected_at = None
    draft.save()
    return draft


# ── confirmation ─────────────────────────────────────────────────


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def issue_rebuild_token(draft: PlannerDraft) -> str:
    """Authorise ONE rebuild of exactly these inputs.

    Only for a draft that is actually rebuilding: keeping the current sections adds
    courses around what the student already has and destroys nothing, so asking them
    to confirm it would be a dialog that teaches them to click through dialogs.
    """
    _require_live(draft)
    if draft.keep_current_sections:
        raise DraftError("Keeping the current sections needs no confirmation.")

    raw = secrets.token_urlsafe(32)
    draft.rebuild_token_hash = _hash(raw)
    draft.rebuild_token_version = draft.version
    draft.rebuild_token_expires_at = timezone.now() + TOKEN_TTL
    draft.save(
        update_fields=[
            "rebuild_token_hash",
            "rebuild_token_version",
            "rebuild_token_expires_at",
        ]
    )
    # Returned once. Only the hash is stored, so a database reader cannot confirm
    # a rebuild on the student's behalf.
    return raw


def _confirmation_is_valid(draft: PlannerDraft, raw: str | None) -> bool:
    if not raw or not draft.rebuild_token_hash:
        return False
    if draft.rebuild_token_version != draft.version:
        return False
    if not draft.rebuild_token_expires_at or draft.rebuild_token_expires_at <= timezone.now():
        return False
    return secrets.compare_digest(draft.rebuild_token_hash, _hash(raw))


# ── generation ───────────────────────────────────────────────────


def generation_fingerprint(
    *,
    version: int,
    academic_year: str,
    term: str,
    course_codes: list[str],
    fixed_sections: dict[str, int],
    keep_current_sections: bool,
    baseline: list[dict[str, Any]],
) -> str:
    """A deterministic identity for one set of inputs.

    No timestamps: two identical requests must fingerprint the same, or the value
    answers "when did this run" instead of "what did it run on". The baseline is in
    it because a student whose registrations changed is asking a different question
    with the same words, and the term is in it because it decides what the baseline
    even contains.
    """
    material = {
        "schema": GENERATION_SCHEMA_VERSION,
        "version": int(version),
        "term": f"{academic_year}/{term}",
        "courses": sorted(course_codes),
        "fixed": {k: int(v) for k, v in sorted(fixed_sections.items())},
        "keep_current_sections": bool(keep_current_sections),
        "baseline": sorted(
            f"{m.get('course_code')}|{m.get('section')}|{m.get('day')}|"
            f"{m.get('start_time')}|{m.get('end_time')}"
            for m in baseline
        ),
    }
    return hashlib.sha256(
        json.dumps(material, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def generate(draft: PlannerDraft, *, confirmation: str | None = None) -> PlannerDraft:
    """Produce and persist the alternatives for this draft's current version.

    Idempotent by version under a row lock: a second request for a version that
    already has a result returns that result rather than running the solver again.
    Two tabs otherwise produce two different sets of timetables, and the student
    compares one while looking at the other.

    The confirmation is consumed only once the generation is persisted. Both live in
    the same transaction, so a solver failure rolls back the consumption too and the
    student can retry without asking for permission a second time.
    """
    from core.services.student_sections import get_student_term_baseline

    with transaction.atomic():
        locked = PlannerDraft.objects.select_for_update().get(pk=draft.pk)
        _require_live(locked)

        if locked.has_current_generation:
            # Someone else generated this exact version while we waited for the lock.
            return locked

        if not locked.keep_current_sections and not _confirmation_is_valid(locked, confirmation):
            raise ConfirmationRequired(
                "Rebuilding replaces the sections you are registered in. Confirm first."
            )

        # Revalidated at generation time, not trusted from when the draft was made:
        # a section can be withdrawn, and a student can change programme, between
        # the draft being created and being acted on.
        codes, pins = validate_draft_selection(
            locked.student_id, locked.course_codes, locked.fixed_sections
        )
        # From the DRAFT, never the request: see the field comment on the model.
        year, term = locked.academic_year, locked.term
        baseline = get_student_term_baseline(locked.student_id, year, term)

        result = build_student_options(
            PlannerRequest(
                student_id=locked.student_id,
                year=int(year),
                term=int(term),
                must_include=tuple(codes),
                keep_current_sections=locked.keep_current_sections,
                fixed_sections=tuple(pins.items()),
            )
        )

        locked.alternatives = result["alternatives"]
        locked.generated_inputs = {
            "version": locked.version,
            "course_codes": codes,
            "fixed_sections": pins,
            "keep_current_sections": locked.keep_current_sections,
            "baseline": baseline,
            "academic_year": year,
            "term": term,
            "unplaced": result["unplaced"],
            "generated_count": result["generated"],
            "fingerprint": generation_fingerprint(
                version=locked.version,
                academic_year=year,
                term=term,
                course_codes=codes,
                fixed_sections=pins,
                keep_current_sections=locked.keep_current_sections,
                baseline=baseline,
            ),
        }
        locked.generated_at = timezone.now()
        locked.generation_schema_version = GENERATION_SCHEMA_VERSION
        # Consumed only now — after the solver returned and the result is about to
        # be committed alongside it.
        locked.rebuild_token_hash = ""
        locked.rebuild_token_version = 0
        locked.rebuild_token_expires_at = None
        locked.save()
        return locked


def select_alternative(draft: PlannerDraft, key: str) -> PlannerDraft:
    """Record a preference. NOT a registration, and nothing here writes one.

    The key is resolved against the alternatives stored on THIS draft's current
    generation, so a client cannot name a timetable that was never offered — or one
    from a generation the student has since edited away from.
    """
    _require_live(draft)
    if not draft.has_current_generation:
        raise DraftError("There are no current alternatives to choose from.")

    offered = {str(a.get("key")) for a in draft.alternatives}
    if str(key) not in offered:
        raise DraftError("That timetable is not one of the alternatives offered.")

    draft.selected_alternative = str(key)
    draft.selected_at = timezone.now()
    draft.save(update_fields=["selected_alternative", "selected_at"])
    return draft


def _require_live(draft: PlannerDraft) -> None:
    if not draft.is_live:
        raise DraftExpired(
            "This planner draft has expired. Start again from your courses — the "
            "sections on file may have changed since it was made."
        )


__all__ = [
    "ConfirmationRequired",
    "DraftError",
    "DraftExpired",
    "DraftRejected",
    "GENERATION_SCHEMA_VERSION",
    "create_draft",
    "edit_draft",
    "generate",
    "generation_fingerprint",
    "issue_rebuild_token",
    "owned_draft",
    "planning_term",
    "select_alternative",
]
