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


def credit_ceiling(term: int) -> int:
    """The most credit hours a suggested timetable may carry.

    `build_plans` treats `max_credits=0` as NO LIMIT, and the request object
    defaults to 0 — so leaving it unset does not mean "use a sensible default", it
    means "unbounded". Naming enough courses produced 22-credit timetables against
    a regulation that stops at 19, on a screen whose whole purpose is to show a
    student what they could register for.

    The number is not invented here. `credit_policy` already owns both figures and
    the reason they differ, including why the summer one is a bound this code may
    apply but not a limit the adviser may quote.
    """
    from core.services.credit_policy import (
        MAIN_TERMS,
        REGULATORY_MAX_CREDITS,
        SUMMER_MAX_CREDITS_BOUND,
    )

    return REGULATORY_MAX_CREDITS if int(term) in MAIN_TERMS else SUMMER_MAX_CREDITS_BOUND


class DraftError(Exception):
    """The draft cannot be used this way."""


class DraftExpired(DraftError):
    """Too old to act on."""


class ConfirmationRequired(DraftError):
    """Rebuilding discards the student's own section choices; it must be confirmed."""


class DraftConflict(DraftError):
    """Someone else changed this draft between our read and our write."""


def _lock(draft: PlannerDraft) -> PlannerDraft:
    """Re-read the row under a lock, inside the caller's transaction.

    `select_for_update` is REAL on PostgreSQL, which is what production runs, and
    silently a no-op on SQLite, which is what the dev database and the whole test
    suite run — Django's compiler nests the "are we in a transaction" check inside
    a `has_select_for_update` feature flag, so the request is discarded without
    error. Every rule that would rest on this lock alone therefore carries a
    conditional UPDATE as well. This is the fast path, not the guarantee.
    """
    return PlannerDraft.objects.select_for_update().get(pk=draft.pk)


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

    Re-read under a lock and written with an explicit version guard. Editing was
    the one mutator that read, decided and wrote from an unlocked snapshot, and
    that defeats the very property the version exists for: two tabs editing at once
    both computed `version + 1` from version 1, both wrote 2, and the row ended up
    holding one tab's courses at the version number the OTHER tab was displaying.
    The student then confirms what is on their screen, the token binds to version 2,
    the version check passes — and the rebuild runs on a course set they were never
    shown. No token rule can catch that, because nothing about the token was wrong.
    """
    with transaction.atomic():
        locked = _lock(draft)
        _require_live(locked)

        codes = locked.course_codes if course_codes is None else course_codes
        pins = locked.fixed_sections if fixed_sections is None else fixed_sections
        validated_codes, validated_pins = validate_draft_selection(locked.student_id, codes, pins)
        keep = (
            locked.keep_current_sections
            if keep_current_sections is None
            else bool(keep_current_sections)
        )

        unchanged = (
            validated_codes == list(locked.course_codes or [])
            and validated_pins == dict(locked.fixed_sections or {})
            and keep == locked.keep_current_sections
        )
        if unchanged:
            # Not a no-op for politeness: re-posting an unchanged selection must not
            # bump the version, or the screen's own "save" would kill a confirmation
            # the student had just been given.
            return locked

        updated = PlannerDraft.objects.filter(pk=locked.pk, version=locked.version).update(
            course_codes=validated_codes,
            fixed_sections=validated_pins,
            keep_current_sections=keep,
            version=locked.version + 1,
            # Everything downstream of the inputs dies with them.
            rebuild_token_hash="",
            rebuild_token_version=0,
            rebuild_token_expires_at=None,
            alternatives=[],
            generated_inputs={},
            generated_version=0,
            generated_at=None,
            generation_schema_version="",
            selected_alternative="",
            selected_at=None,
        )
        if not updated:
            # Another writer moved the version between our read and our write. On a
            # backend with real row locks this cannot happen; on SQLite, where
            # `select_for_update` is silently a no-op, this guard is the whole
            # defence. Refuse rather than overwrite: the caller re-reads and the
            # student is shown what the draft actually says.
            raise DraftConflict(
                "تغيّرت مساحة التخطيط في نافذة أخرى. أعد تحميل الصفحة، ثم حاول مرة أخرى."
            )
        locked.refresh_from_db()
        return locked


# ── confirmation ─────────────────────────────────────────────────


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def issue_rebuild_token(draft: PlannerDraft) -> str:
    """Authorise ONE rebuild of exactly these inputs.

    Only for a draft that is actually rebuilding: keeping the current sections adds
    courses around what the student already has and destroys nothing, so asking them
    to confirm it would be a dialog that teaches them to click through dialogs.
    """
    with transaction.atomic():
        locked = _lock(draft)
        _require_live(locked)
        if locked.keep_current_sections:
            raise DraftError("الاحتفاظ بشُعب الجدول المرجعي لا يتطلب تأكيدًا.")

        raw = secrets.token_urlsafe(32)
        # Guarded on the version it is being bound to: a token must never be issued
        # for inputs that changed between the student reading the warning and the
        # server writing the permission.
        updated = PlannerDraft.objects.filter(pk=locked.pk, version=locked.version).update(
            rebuild_token_hash=_hash(raw),
            rebuild_token_version=locked.version,
            rebuild_token_expires_at=timezone.now() + TOKEN_TTL,
        )
        if not updated:
            raise DraftConflict(
                "تغيّرت مساحة التخطيط في نافذة أخرى. أعد تحميل الصفحة، ثم حاول مرة أخرى."
            )
        draft.refresh_from_db()
    # Returned once. Only the hash is stored, so a database reader cannot confirm
    # a rebuild on the student's behalf.
    return raw


def _confirmation_is_valid(draft: PlannerDraft, raw: Any) -> bool:
    # Type-checked, not just truth-checked. A JSON body may carry any type, and
    # `_hash` would reach `True.encode` — turning a refusal into a 500. Every
    # non-string is simply not the token.
    if not isinstance(raw, str) or not raw or not draft.rebuild_token_hash:
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


def generation_is_stale(draft: PlannerDraft) -> bool:
    """Whether the world moved under a generation that is still being shown.

    The version catches the student changing their own selection. It cannot catch
    the OTHER thing that invalidates a set of timetables: their registrations
    changing. An adviser adds or drops a section, and the stored alternatives were
    built around a baseline that no longer exists — for up to `DRAFT_TTL`, with
    nothing on the screen saying so.

    This is what the fingerprint was for. It was computed over exactly this
    material, written into `generated_inputs`, and then never compared to anything,
    which made it a value that described the problem instead of detecting it.
    """
    from core.services.student_sections import get_student_term_baseline
    from core.services.timetable_snapshots import Snapshot

    if not draft.has_current_generation:
        return False
    stored = str((draft.generated_inputs or {}).get("fingerprint") or "")
    if not stored:
        # A row from before the fingerprint existed. Unknown is not stale.
        return False
    inputs = draft.generated_inputs or {}
    current = generation_fingerprint(
        version=draft.version,
        academic_year=draft.academic_year,
        term=draft.term,
        course_codes=list(inputs.get("course_codes") or []),
        fixed_sections=dict(inputs.get("fixed_sections") or {}),
        keep_current_sections=bool(inputs.get("keep_current_sections")),
        baseline=get_student_term_baseline(
            draft.student_id, draft.academic_year, draft.term, snapshot=Snapshot.EFFECTIVE
        ),
    )
    return current != stored


def generate(draft: PlannerDraft, *, confirmation: Any = None) -> PlannerDraft:
    """Produce and persist the alternatives for this draft's current version.

    Idempotent by version: a second request for a version that already has a result
    returns that result rather than running the solver again. Two tabs otherwise
    produce two different sets of timetables, and the student compares one while
    looking at the other.

    That is enforced by CLAIMING the version with a conditional UPDATE before the
    solver runs, not by the row lock alone — `select_for_update` is real on
    PostgreSQL and silently nothing on SQLite, so a rule resting on it would hold
    in production and merely be hoped for everywhere the tests run. The claim is
    inside the transaction, so a solver failure releases it along with everything
    else, and on SQLite it takes the single write lock at the moment it runs, which
    is what actually serialises two concurrent requests there.

    The confirmation is consumed only once the generation is persisted. Both live in
    the same transaction, so a solver failure rolls back the consumption too and the
    student can retry without asking for permission a second time.
    """
    from core.services.student_sections import get_student_term_baseline
    from core.services.timetable_snapshots import Snapshot

    with transaction.atomic():
        locked = _lock(draft)
        _require_live(locked)

        if locked.has_current_generation:
            # Someone else already generated this exact version.
            return locked

        if not locked.keep_current_sections and not _confirmation_is_valid(locked, confirmation):
            raise ConfirmationRequired(
                "ستُنشئ إعادة البناء جدولًا مقترحًا جديدًا من البداية، بدل الاحتفاظ "
                "بالجدول المرجعي المعروض أعلاه، مع الإبقاء على الشُعب التي ثبّتها "
                "يدويًا. يجب تأكيد هذا الإجراء أولًا."
            )

        # THE claim. One statement, so it is atomic on every backend: whoever moves
        # `generated_version` to this version owns the generation. A loser sees zero
        # rows updated and serves the winner's result rather than running a second
        # solve and overwriting it.
        claimed = PlannerDraft.objects.filter(
            pk=locked.pk, version=locked.version, generated_version__lt=locked.version
        ).update(generated_version=locked.version)
        if not claimed:
            locked.refresh_from_db()
            return locked

        # Revalidated at generation time, not trusted from when the draft was made:
        # a section can be withdrawn, and a student can change programme, between
        # the draft being created and being acted on.
        codes, pins = validate_draft_selection(
            locked.student_id, locked.course_codes, locked.fixed_sections
        )
        # From the DRAFT, never the request: see the field comment on the model.
        year, term = locked.academic_year, locked.term
        if not (year.isdigit() and term.isdigit()):
            # `create_draft` always fills these, but the columns carry a blank
            # default, so a fixture, the admin, or any future writer can leave a row
            # that fails `int("")` two lines below — surfacing as a 500 rather than
            # as anything a caller can act on.
            raise DraftError("لم يُحدّد الفصل الدراسي لمساحة التخطيط. افتح أداة التخطيط من جديد.")
        baseline = get_student_term_baseline(
            locked.student_id, year, term, snapshot=Snapshot.EFFECTIVE
        )

        result = build_student_options(
            PlannerRequest(
                student_id=locked.student_id,
                year=int(year),
                term=int(term),
                must_include=tuple(codes),
                keep_current_sections=locked.keep_current_sections,
                fixed_sections=tuple(pins.items()),
                max_credits=credit_ceiling(int(term)),
                # The draft is the student's exact on-screen selection.  Its
                # initial value already comes from the recommender when no
                # explicit courses were supplied; silently adding removed
                # recommendations back here made the picker impossible to use.
                include_recommendations=False,
                # Use the exact provenance-resolved snapshot fingerprinted and
                # stored below.  Without this override the adapter performs a
                # second read, allowing a registrar scrape arriving between the
                # two reads to make the solver use a different baseline from the
                # one the draft says it used.
                baseline_override=tuple(dict(row) for row in baseline),
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
        locked.generated_version = locked.version
        locked.generation_schema_version = GENERATION_SCHEMA_VERSION
        # Consumed only now — after the solver returned and the result is about to
        # be committed alongside it.
        locked.rebuild_token_hash = ""
        locked.rebuild_token_version = 0
        locked.rebuild_token_expires_at = None
        locked.save()
        return locked


def select_alternative(draft: PlannerDraft, key: Any) -> PlannerDraft:
    """Record a preference. NOT a registration, and nothing here writes one.

    The key is resolved against the alternatives stored on THIS draft's current
    generation, so a client cannot name a timetable that was never offered — or one
    from a generation the student has since edited away from.

    Locked and version-guarded like every other mutator: read unlocked, the
    membership check and the write straddle a concurrent generation, and the
    preference survives pointing at a timetable that is no longer on offer.
    """
    with transaction.atomic():
        locked = _lock(draft)
        _require_live(locked)
        if not locked.has_current_generation:
            raise DraftError("لا توجد جداول مقترحة معروضة للاختيار منها حاليًا.")

        offered = {str(a.get("key")) for a in locked.alternatives}
        if str(key) not in offered:
            raise DraftError("هذا الجدول ليس ضمن الجداول المقترحة المعروضة حاليًا.")

        updated = PlannerDraft.objects.filter(
            pk=locked.pk, version=locked.version, generated_version=locked.generated_version
        ).update(selected_alternative=str(key), selected_at=timezone.now())
        if not updated:
            raise DraftConflict(
                "تغيّرت الجداول المقترحة أثناء اختيارك. أعد تحميل الصفحة، ثم اختر من جديد."
            )
        locked.refresh_from_db()
        return locked


def _require_live(draft: PlannerDraft) -> None:
    if not draft.is_live:
        raise DraftExpired(
            "انتهت صلاحية مساحة التخطيط. افتح أداة التخطيط من جديد من قائمة "
            "مقرراتك؛ فقد تكون بيانات الشُعب قد تغيّرت منذ فتح المساحة السابقة."
        )


__all__ = [
    "ConfirmationRequired",
    "DraftConflict",
    "DraftError",
    "DraftExpired",
    "DraftRejected",
    "GENERATION_SCHEMA_VERSION",
    "create_draft",
    "credit_ceiling",
    "edit_draft",
    "generate",
    "generation_fingerprint",
    "generation_is_stale",
    "issue_rebuild_token",
    "owned_draft",
    "planning_term",
    "select_alternative",
]
