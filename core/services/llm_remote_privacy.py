"""What may leave the institution, decided per capability, by allowlist.

An external provider is a data processor. The adviser's tool results were built
for an internal audience — the UI evidence panel, the citation checker, the audit
record — and they carry what that audience needs: student ids, names, adviser
names and email addresses, instructor names, and operator-facing notes on why a
policy may be used. None of that has any business crossing an institutional
boundary, and most of it is not needed to answer the question.

WHY ALLOWLISTS AND NOT A KEY BLACKLIST

A recursive scrubber that deletes keys called `student_id`, `name`, `email` looks
like it works and fails silently on the first shape nobody anticipated —
`advisor_email`, `meetings[].instructor`, `eligible_student_ids_sample`,
`runtime_use_note`. It passes unknown data through BECAUSE the key name was
unexpected, which is precisely backwards: an unrecognised shape is the case that
most needs stopping.

So every capability has an explicit projector, and a capability with no projector
raises before serialisation. Unknown fails closed by construction rather than by
vigilance.

THE MODEL NEVER NEEDS A STUDENT'S IDENTITY

Session-bound tools resolve the student from `scope` and `principal`, server-side,
after authorisation. The identity in a tool result is an echo the model does not
act on — so it is removed, and where a model genuinely must distinguish students
(adviser mode) it gets request-scoped `STUDENT_n` references it cannot turn back
into a person.

MEASURED, NOT ASSUMED

Thirteen capability inventories were read from the executors' actual return
statements. Eleven of thirteen tool SCHEMAS advertise a `student_id` parameter,
which is its own problem: a schema that names a real identity invites the model
to echo or invent one even when every result is projected. `remote_tool_schemas`
strips those parameters for remote mode.
"""

from __future__ import annotations

import logging
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from core.services.llm_backend import LLMPrivacyError

logger = logging.getLogger(__name__)


class RemoteExposure(Enum):
    """How a capability may be exposed to an external provider.

    NOTE THAT `ALLOW` IS NOT PASS-THROUGH. Every remotely usable capability goes
    through an exact projector, including the ones that carry no identity today.
    A raw pass-through is a standing promise that nobody will ever add a field —
    and provenance, staff attribution and internal metadata are exactly the kind
    of thing that gets added to a "harmless" result later, at which point it
    starts being transmitted automatically and silently.
    """

    #: Remotely usable and carries no identity — still projected, field by field.
    ALLOW = "allow"
    #: Useful remotely, reduced to an explicit allowlist.
    PROJECT = "project"
    #: Genuinely needs to distinguish students; gets `STUDENT_n` references.
    OPAQUE_IDENTITIES = "opaque_identities"
    #: Never advertised, never executed, for a remote backend.
    DENY = "deny"


#: Every capability the registry can expose, with a deliberate decision.
#: A capability missing from this map cannot be sent remotely — see
#: `project_tool_result_for_remote`.
REMOTE_POLICY: dict[str, RemoteExposure] = {
    # ── clean: course reference data, no person in it ──
    "lookup_course": RemoteExposure.ALLOW,
    "course_prerequisites": RemoteExposure.ALLOW,
    # ── session-bound: echo the caller's own id back, nothing else personal ──
    "my_progress": RemoteExposure.PROJECT,
    "why_course_locked": RemoteExposure.PROJECT,
    "graduation_progress": RemoteExposure.PROJECT,
    "my_plan_by_term": RemoteExposure.PROJECT,
    "recommend_courses": RemoteExposure.PROJECT,
    "my_clash_free_sections": RemoteExposure.PROJECT,
    "build_my_timetable": RemoteExposure.PROJECT,
    "build_timetable_proposal": RemoteExposure.PROJECT,
    # ── carries staff names ──
    "my_timetable": RemoteExposure.PROJECT,
    "my_advisor": RemoteExposure.PROJECT,
    # ── policy text plus operator-facing notes ──
    "policy_lookup": RemoteExposure.PROJECT,
    # ── the whole student record ──
    "get_student_context": RemoteExposure.OPAQUE_IDENTITIES,
    # ── adviser/multi-student ──
    "find_students": RemoteExposure.OPAQUE_IDENTITIES,
    #: Its entire purpose is `risk_score` and `needs_attention` — adviser
    #: judgement about named students. There is no projection of that worth
    #: sending; the honest answer is that it does not go.
    "portfolio_triage": RemoteExposure.DENY,
    "aggregate_demand": RemoteExposure.DENY,
    "course_eligibility": RemoteExposure.DENY,
    "graduation_shortfall": RemoteExposure.DENY,
}

#: A refused capability says nothing about WHY. "portfolio_triage contains
#: risk_score" tells a model — and anyone reading a transcript — what the field
#: is called and that it exists.
DENIED_RESULT: dict[str, Any] = {
    "ok": False,
    "error_code": "CAPABILITY_NOT_AVAILABLE_FOR_REMOTE_BACKEND",
}

#: Every OTHER boundary refusal — a forged identity argument, an unknown or stale
#: reference, a failed scope re-check, a result whose shape the projector will not
#: accept. One message for all of them on purpose.
#:
#: DENY may name itself: which capabilities exist remotely is already visible in
#: the schema list the model was given, so saying so discloses nothing. The rest
#: must not be told apart. "no such reference" and "not authorised" as separate
#: answers is a directory oracle: ask about 4502157, read which refusal comes
#: back, and the boundary has confirmed a real student exists.
REFUSED_RESULT: dict[str, Any] = {
    "ok": False,
    "error_code": "TOOL_CALL_REFUSED",
    "error": "This tool call could not be completed.",
}

#: Parameters that name a real identity. Stripped from remote schemas: the server
#: resolves the student from the session, so advertising the field only invites
#: the model to supply one — and a model that can pass a student id is a model
#: that can pass someone else's.
IDENTITY_PARAMETERS = frozenset({"student_id", "advisor_id", "student_ids", "email"})


class UnverifiedIdentity(LLMPrivacyError):
    """A number that looks like a student id but could not be verified.

    Deliberately fatal to the request. The alternative — aliasing it anyway —
    manufactures a resolvable reference for something that may be a transaction
    number, an order reference, or nothing at all, and then tells the model it is
    a student. Refusing locally costs one error message; guessing costs the
    integrity of every downstream claim.
    """


#: 128 bits. The first version used two bytes, and 16 bits is not a secret: one
#: guess in 65,536 recovers the active prefix, and an attacker gets a guess per
#: question. The nonce is the ONLY thing standing between a typed `STUDENT_1` and
#: a real person's record, so it is sized like the credential it is rather than
#: like a display string. The cost is prompt tokens on a reference the model
#: rarely repeats — the wrong side of that trade is not close.
_NONCE_BYTES = 16

#: An injected nonce (tests) must still be a plausible one. A deterministic
#: factory is the right way to make references assertable; an EMPTY one would
#: silently rebuild the forgeable scheme this replaced.
_NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{8,}$")


def _new_nonce() -> str:
    return secrets.token_hex(_NONCE_BYTES).upper()


@dataclass(repr=False)
class RemoteIdentityMap:
    """Opaque references, scoped to ONE `answer_virtual_advisor` execution.

    A reference means nothing outside the answer that minted it, and it carries a
    per-request NONCE so it cannot be forged from outside either. Without the
    nonce a student could type "STUDENT_1" into their question and have it
    resolve to whoever the map happened to number first — an impersonation with
    no exploit required beyond guessing an obvious string.

    Never persisted, never logged, never serialised into a trace.
    """

    #: Unguessable, and different every answer. See `_NONCE_BYTES`.
    nonce: str = field(default_factory=_new_nonce)
    _to_ref: dict[int, str] = field(default_factory=dict)
    _to_id: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not _NONCE_RE.match(self.nonce or ""):
            raise LLMPrivacyError(
                "a request nonce must be at least 8 URL-safe characters; "
                "refusing to issue guessable references."
            )

    def reference_for(self, student_id: int | str) -> str:
        """Mint or reuse a reference. CALL ONLY AFTER AUTHORISATION.

        This function does not check anything — it cannot, it has no scope. The
        caller establishes that the student exists and that the principal may see
        them, and this records the mapping. Stable within one answer, so the
        model can reason about "the second one" coherently.
        """
        try:
            real = int(str(student_id).strip())
        except (TypeError, ValueError) as exc:
            raise LLMPrivacyError(
                "a student id that is not an integer cannot be referenced"
            ) from exc
        if real not in self._to_ref:
            ref = f"STUDENT_REF_{self.nonce}_{len(self._to_ref) + 1}"
            self._to_ref[real] = ref
            self._to_id[ref] = real
        return self._to_ref[real]

    def resolve(self, reference: str) -> int:
        """Reference → real id, or refuse.

        Fails closed on anything this map did not mint: an unknown reference, one
        from an earlier answer (different nonce), a forged one, and a real
        numeric id all raise. The last matters most — a model that can pass
        `student_id=4502156` remotely has bypassed the entire scheme.
        """
        ref = str(reference or "").strip()
        if ref not in self._to_id:
            raise LLMPrivacyError(
                "that reference was not issued during this answer; refusing to resolve it."
            )
        return self._to_id[ref]

    def issued(self, reference: str) -> bool:
        return str(reference or "").strip() in self._to_id

    def __len__(self) -> int:
        return len(self._to_ref)

    def __bool__(self) -> bool:
        """Always true. `__len__` alone makes a map that has issued nothing
        FALSY, and `identities or RemoteIdentityMap()` then quietly replaces the
        caller's map with a fresh one — after which every reference already
        minted fails to resolve and the failure looks like forgery. An identity
        map is a collaborator, not a container: its emptiness says nothing about
        whether it is there."""
        return True

    def __repr__(self) -> str:
        # Never print the mapping, and never the nonce: together they are the one
        # thing that turns a reference back into a person.
        return f"<RemoteIdentityMap {len(self._to_ref)} reference(s)>"


# ── per-capability projectors ────────────────────────────────────
#
# Each returns a NEW dict built field by field. None of them mutates or copies
# its input, so a field added to a capability tomorrow is absent from the remote
# payload until somebody adds it here on purpose.


def _keep(source: dict[str, Any], *names: str) -> dict[str, Any]:
    """Copy only these keys, and only if present."""
    return {name: source[name] for name in names if name in source}


def _envelope(result: dict[str, Any]) -> dict[str, Any]:
    """`ok`/`error`/`tool` are control flow, not data. `error` is included
    because the model must know a call failed — the executors' error strings are
    fixed messages about arguments and scope, not about people."""
    return _keep(result, "tool", "ok", "error")


def _course_rows(rows: Any, *fields: str) -> list[dict[str, Any]]:
    if not isinstance(rows, list):
        return []
    return [_keep(row, *fields) for row in rows if isinstance(row, dict)]


def _project_lookup_course(result: dict[str, Any], _: RemoteIdentityMap) -> dict[str, Any]:
    """Course reference data. No person in it today — and this projector is what
    keeps that true when somebody adds `created_by` or `last_edited_by`."""
    out = _envelope(result)
    out.update(_keep(result, "query", "match_count"))
    out["courses"] = _course_rows(
        result.get("courses"), "course_code", "course_name", "credit_hours", "programs"
    )
    return out


def _project_course_prerequisites(result: dict[str, Any], _: RemoteIdentityMap) -> dict[str, Any]:
    out = _envelope(result)
    out.update(_keep(result, "course_code", "note", "is_elective_placeholder"))
    out["options"] = _course_rows(
        result.get("options"), "course_code", "course_name", "credit_hours", "prerequisites"
    )
    per_program = result.get("per_program")
    if isinstance(per_program, list):
        out["per_program"] = [
            _keep(row, "program", "prerequisites", "course_name", "programme_term", "credit_hours")
            for row in per_program
            if isinstance(row, dict)
        ]
    return out


#: The impact row's four keys, named once because `most_useful_course_to_pass`
#: and every row of `unlock_impact_ranking` are the same shape.
_IMPACT_FIELDS = (
    "code",
    "course_name",
    "sole_remaining_prerequisite_count",
    "on_prerequisite_chain_of_count",
)


def _project_my_progress(result: dict[str, Any], _: RemoteIdentityMap) -> dict[str, Any]:
    """WRITTEN AGAINST A PAYLOAD `_exec_my_progress` HAS NEVER EMITTED.

    It kept `totals`, `programme_totals`, `passed`, `studying` and `remaining`. The
    executor emits none of those five: it emits `counts`, `most_useful_course_to_pass`,
    the two readiness lists, `elective_slots` and `note`. `_keep` is silent about a
    name that is not there and `_course_rows` returns `[]` for a list that is not
    there, so the projection did not fail — it produced `{ok, counts, passed: [],
    studying: [], remaining: []}` and looked like a filled payload.

    That is why the live batch's priority questions were answered without the
    evidence: on the remote backend the model received a bucket of five numbers and
    three empty lists, and every course name, count and ranking in the answer had to
    come from somewhere other than this tool.

    None of the added fields carries identity. They are course codes, course names,
    plan levels and counts over the student's own plan — the same class of data the
    `counts` bucket already carried, and the student is the caller.
    """
    out = _envelope(result)
    out.update(
        _keep(
            result,
            "program",
            "academic_year",
            "term",
            "counts",
            "elective_slots",
            "renamed_fields",
            "note",
        )
    )
    top = result.get("most_useful_course_to_pass")
    out["most_useful_course_to_pass"] = (
        _keep(top, *_IMPACT_FIELDS) if isinstance(top, dict) else None
    )
    out["unlock_impact_ranking"] = _course_rows(
        result.get("unlock_impact_ranking"), *_IMPACT_FIELDS
    )
    out["prerequisites_satisfied"] = _course_rows(
        result.get("prerequisites_satisfied"),
        "code",
        "course_name",
        "credits",
        "fits_this_term",
    )
    out["prerequisite_blocked"] = _course_rows(
        result.get("prerequisite_blocked"),
        "code",
        "course_name",
        "steps_away",
        "on_prerequisite_chain_of_count",
        "nearest_course_you_can_take_now",
        "why",
    )
    return out


_GRADUATION_BLOCKER_FIELDS = (
    "code",
    "name",
    "credits",
    "requirement_type",
    "elective_slot",
    "missing_course_prerequisites",
    "missing_prerequisites_outside_plan",
    "credit_hour_gate",
)


def _project_graduation_summary(summary: Any) -> dict[str, Any] | None:
    if not isinstance(summary, dict):
        return None
    out = _keep(
        summary,
        "simulation_completed",
        "estimated_additional_terms",
        "estimated_terms_including_current",
        "lower_bound_additional_terms",
        "lower_bound_terms_including_current",
        "registered_credits_now",
    )
    out["current_courses_assumed_passed"] = _course_rows(
        summary.get("current_courses_assumed_passed"), "code", "name", "credits"
    )
    out["unresolved_requirements"] = _course_rows(
        summary.get("unresolved_requirements"), *_GRADUATION_BLOCKER_FIELDS
    )
    return out


def _project_graduation_comparison(comparison: Any) -> dict[str, Any] | None:
    if not isinstance(comparison, dict):
        return None
    out = _keep(
        comparison,
        "timing_effect",
        "term_difference",
        "terms_saved",
        "exact_timing_comparison_available",
        "proven_improvement",
        "complete_forecast_improved",
        "blocker_progress_only",
        "improvement_basis",
        "baseline_lower_bound_additional_terms",
        "scenario_lower_bound_additional_terms",
        "lower_bound_change",
        "baseline_current_credits",
        "scenario_current_credits",
        "current_credit_change",
    )
    out["blockers_resolved"] = _course_rows(
        comparison.get("blockers_resolved"), *_GRADUATION_BLOCKER_FIELDS
    )
    out["blockers_improved"] = _course_rows(
        comparison.get("blockers_improved"),
        "code",
        "prerequisites_resolved",
        "credit_gap_reduced_by",
        "credit_gap_remaining",
    )
    out["blockers_introduced"] = _course_rows(
        comparison.get("blockers_introduced"), *_GRADUATION_BLOCKER_FIELDS
    )
    out["deferred_courses"] = _course_rows(
        comparison.get("deferred_courses"),
        "code",
        "future_sequence",
        "academic_year",
        "term",
        "unresolved",
    )
    return out


def _project_graduation_what_if(what_if: Any) -> dict[str, Any] | None:
    if not isinstance(what_if, dict):
        return None
    out = _keep(
        what_if,
        "mode",
        "valid",
        "timetable_check_required",
        "candidate_courses_considered",
        "pairs_evaluated",
        "search_truncated",
        "unproven_blocker_progress_pairs",
        "no_proven_improvement",
        "note",
    )
    out["validation_errors"] = _course_rows(
        what_if.get("validation_errors"),
        "kind",
        "course_code",
        "course_codes",
        "missing_prerequisites",
        "required",
        "effective",
        "remaining",
        "credits",
        "maximum",
        "maximum_per_list",
    )
    out["removed_current_courses"] = _course_rows(
        what_if.get("removed_current_courses"), "code", "name", "credits"
    )
    out["added_current_courses"] = _course_rows(
        what_if.get("added_current_courses"),
        "code",
        "name",
        "credits",
        "in_degree_plan",
    )
    out["outside_plan_additions"] = _course_rows(
        what_if.get("outside_plan_additions"),
        "code",
        "name",
        "credits",
        "in_degree_plan",
    )
    out["baseline"] = _project_graduation_summary(what_if.get("baseline"))
    out["scenario"] = _project_graduation_summary(what_if.get("scenario"))
    out["comparison"] = _project_graduation_comparison(what_if.get("comparison"))
    out["improving_replacements"] = []
    for replacement in what_if.get("improving_replacements") or []:
        if not isinstance(replacement, dict):
            continue
        projected = _keep(replacement, "outside_plan_addition")
        projected["remove_course"] = _keep(
            replacement.get("remove_course") or {}, "code", "name", "credits"
        )
        projected["add_course"] = _keep(
            replacement.get("add_course") or {},
            "code",
            "name",
            "credits",
            "in_degree_plan",
        )
        projected["comparison"] = _project_graduation_comparison(replacement.get("comparison"))
        projected["scenario"] = _project_graduation_summary(replacement.get("scenario"))
        out["improving_replacements"].append(projected)
    return out


def _project_graduation_progress(result: dict[str, Any], _: RemoteIdentityMap) -> dict[str, Any]:
    out = _envelope(result)
    out.update(
        _keep(
            result,
            "program",
            "plan_courses_passed",
            "plan_courses_total",
            "percent_complete",
            "courses_remaining",
            "credits_remaining_in_plan",
            "credits_earned_registrar",
            "gpa",
            "minimum_terms_by_prerequisites",
            "minimum_terms_by_credit_capacity_after_current",
            "lower_bound_additional_terms",
            "lower_bound_terms_including_current",
            "max_credits_per_term",
            "estimated_additional_terms",
            "estimated_terms_including_current",
            "terms_estimate",
            "simulation_completed",
            "simulated_terms_examined",
            "productive_terms_planned",
            "final_term_possible",
            "passed_credits_in_plan",
            "registered_credits_now",
            "simulation_assumptions",
        )
    )
    out["current_courses_assumed_passed"] = _course_rows(
        result.get("current_courses_assumed_passed"), "code", "name", "credits"
    )
    out["courses_in_progress"] = _course_rows(
        result.get("courses_in_progress"), "code", "name", "credits", "term", "type"
    )
    out["credit_hour_gates"] = _course_rows(
        result.get("credit_hour_gates"),
        "code",
        "name",
        "required",
        "effective",
        "remaining",
    )
    out["unresolved_requirements"] = _course_rows(
        result.get("unresolved_requirements"),
        *_GRADUATION_BLOCKER_FIELDS,
    )
    out["what_if"] = _project_graduation_what_if(result.get("what_if"))
    out["term_plan"] = []
    for term in result.get("term_plan") or []:
        if not isinstance(term, dict):
            continue
        projected = _keep(
            term,
            "sequence",
            "academic_year",
            "term",
            "course_codes",
            "credits",
            "waiting_term",
        )
        projected["courses"] = _course_rows(
            term.get("courses"),
            "code",
            "name",
            "credits",
            "requirement_type",
            "elective_slot",
        )
        out["term_plan"].append(projected)
    return out


def _project_why_course_locked(result: dict[str, Any], _: RemoteIdentityMap) -> dict[str, Any]:
    """Also written against a payload that never existed — see `_project_my_progress`.

    It kept `locked`, `reason`, `missing_prerequisites` and `unlocks`. The executor
    emits `status`, `explanation`, `blocked_by`, `steps_away` and the forward-relation
    fields, and has never emitted any of those four names. So a remote answer about
    one named course received `{ok, course_code}` — the code the student had just
    said out loud — and nothing else. The tool that owns the forward direction was
    contributing no evidence at all on the backend the batch was run against, which
    is a better explanation for reaching to `course_prerequisites` than any missing
    sentence in a description.

    `blocked_by` carries `build_unlock_report`'s reason dicts: a closed `kind`
    vocabulary plus course codes, names and credit-hour figures. No person.
    """
    out = _envelope(result)
    out.update(
        _keep(
            result,
            "course_code",
            "course_name",
            "status",
            "prerequisites_satisfied",
            "explanation",
            "fits_this_term",
            "steps_away",
            "nearest_course_you_can_take_now",
            "blocked_by",
            "listed_as_prerequisite_count",
            "sole_remaining_prerequisite_count",
            "on_prerequisite_chain_of_count",
            "forward_relations_note",
        )
    )
    out["listed_as_prerequisite_for"] = _course_rows(
        result.get("listed_as_prerequisite_for"),
        "code",
        "course_name",
        "current_status",
        "still_also_waiting_on",
        "also_short_on_credit_hours",
    )
    out["sole_remaining_prerequisite_for"] = _course_rows(
        result.get("sole_remaining_prerequisite_for"), "code", "course_name"
    )
    return out


def _project_my_plan_by_term(result: dict[str, Any], _: RemoteIdentityMap) -> dict[str, Any]:
    """Every level, and every COURSE inside it, named field by field.

    `terms` and `plan` used to pass through whole. The module's argument against a
    key blacklist applies just as hard one level down: an allowlist that stops at the
    container passes anything a capability adds inside it, and this container holds a
    row per course built by `report_views` — which is where `importance_score` and
    `missing_prereqs` already live and where the next operator-facing field will.
    """
    out = _envelope(result)
    out.update(_keep(result, "program"))
    terms = result.get("terms")
    if isinstance(terms, list):
        out["terms"] = [
            {
                **_keep(level, "term"),
                "courses": _course_rows(
                    level.get("courses"),
                    "course_code",
                    "type",
                    "programme_term",
                    "credit_hours",
                    "status",
                    "prerequisites",
                    "missing_prereqs",
                    "prerequisites_satisfied",
                ),
            }
            for level in terms
            if isinstance(level, dict)
        ]
    return out


def _project_recommend_courses(result: dict[str, Any], _: RemoteIdentityMap) -> dict[str, Any]:
    out = _envelope(result)
    out.update(_keep(result, "program", "academic_year", "term", "policy", "recommendation_policy"))
    out["recommendations"] = _course_rows(
        result.get("recommendations"), "course_code", "course_name", "credit_hours", "prerequisites"
    )
    return out


def _project_my_clash_free_sections(result: dict[str, Any], _: RemoteIdentityMap) -> dict[str, Any]:
    """Keep safe section evidence for both supported capability payloads.

    The current executor returns ``compared_against_term`` and nested ``courses``;
    older callers can still supply a flat ``sections`` list. Both shapes are built
    field by field. Course codes, section labels, and meeting ranges may leave the
    institution; student identity, rooms, and instructors may not.
    """

    def meeting_rows(rows: Any) -> list[Any]:
        if not isinstance(rows, list):
            return []
        projected: list[Any] = []
        for raw in rows:
            if isinstance(raw, str):
                projected.append(raw)
            elif isinstance(raw, dict):
                projected.append(_keep(raw, "day", "start", "end", "start_time", "end_time"))
        return projected

    def section_rows(rows: Any, *, include_conflicts: bool) -> list[dict[str, Any]]:
        if not isinstance(rows, list):
            return []
        projected = []
        for raw in rows:
            if not isinstance(raw, dict):
                continue
            row = _keep(raw, "section", "is_current_section")
            row["meetings"] = meeting_rows(raw.get("meetings"))
            if include_conflicts:
                row["conflicts"] = _course_rows(
                    raw.get("conflicts"),
                    "section_meeting",
                    "conflicts_with",
                    "registered_meeting",
                )
            projected.append(row)
        return projected

    out = _envelope(result)
    out.update(
        _keep(
            result,
            "academic_year",
            "term",
            "course_code",
            "compared_against_term",
            "note",
        )
    )
    out["courses"] = []
    for raw in result.get("courses") or []:
        if not isinstance(raw, dict):
            continue
        course = _keep(
            raw,
            "course_code",
            "sections_on_file",
            "currently_registered_sections",
            "status",
        )
        course["clash_free"] = section_rows(raw.get("clash_free"), include_conflicts=False)
        course["clashing"] = section_rows(raw.get("clashing"), include_conflicts=True)
        out["courses"].append(course)
    sections = result.get("sections")
    if isinstance(sections, list):
        out["sections"] = [
            {
                **_keep(row, "course_code", "course_name", "section", "status", "reason"),
                "meetings": [
                    _keep(m, "day", "start_time", "end_time")
                    for m in (row.get("meetings") or [])
                    if isinstance(m, dict)
                ],
                "collisions": [
                    _keep(
                        c,
                        "course_code",
                        "day",
                        "start_time",
                        "end_time",
                        "other_start_time",
                        "other_end_time",
                    )
                    for c in (row.get("collisions") or [])
                    if isinstance(c, dict)
                ],
            }
            for row in sections
            if isinstance(row, dict)
        ]
    return out


def _project_build_my_timetable(result: dict[str, Any], _: RemoteIdentityMap) -> dict[str, Any]:
    """The whole provenance contract, plus the sentence that explains a partial result.

    ``note`` was not on the old list, and its absence is why a remote answer could
    report a partial build as a failure: the note is the sentence saying "there is
    nothing to schedule … the plan is complete or every remaining course is
    blocked", and the tool description insists a partial result "must be reported as
    such, never as a failure". The model was held to a rule whose evidence had been
    stripped from the payload it was given. The note carries no identity — every
    branch of the executor writes it as a constant in this repository.

    ``academic_year`` and ``term`` were dead names: the executor emits
    ``using_timetable_of_term`` and has never emitted those two, so a remote answer
    was working from a timetable with no term on it at all.

    The section rows need no filtering HERE because they were built filtered:
    ``timetable_provenance.baseline_sections`` keeps course, name, section and
    meeting times and drops ``instructor`` and ``room``. That is the same rule
    ``_project_my_timetable`` states two functions below, and it is enforced at the
    point the rows are made so that this allowlist cannot be the only thing standing
    between a member of staff's name and an external provider.
    """
    out = _envelope(result)
    out.update(
        _keep(
            result,
            "using_timetable_of_term",
            "student_requested_courses",
            "system_recommended_courses",
            "retained_sections",
            "new_sections",
            "fixed_sections",
            "section_replacements",
            "unplaced_courses",
            "credit_summary",
            "alternatives_considered",
            "note",
            "reason",
            "action",
            "tool",
        )
    )
    return out


def _project_build_timetable_proposal(
    result: dict[str, Any], _: RemoteIdentityMap
) -> dict[str, Any]:
    """Project only the student-safe timetable facts needed to write the answer."""
    out = _envelope(result)
    out.update(
        _keep(
            result,
            "planning_term",
            "mode",
            "student_requested_courses",
            "system_recommended_courses",
            "current_credit_hours",
            "credit_ceiling",
            "alternatives_generated",
            "distinct_alternatives",
            "registration_action",
            "can_save",
            "can_register",
            "note",
        )
    )
    current = result.get("current_sections")
    if isinstance(current, list):
        out["current_sections"] = [
            {
                **_keep(row, "course_code", "course_name", "section", "credits"),
                "meetings": [str(item) for item in (row.get("meetings") or [])],
            }
            for row in current
            if isinstance(row, dict)
        ]
    alternatives = result.get("alternatives")
    if isinstance(alternatives, list):
        out["alternatives"] = []
        for alternative in alternatives:
            if not isinstance(alternative, dict):
                continue
            safe = _keep(
                alternative,
                "option",
                "planner_options",
                "scheduled_courses",
                "target_courses",
                "course_count",
                "proposed_credit_hours",
                "total_credit_hours",
                "days_on_campus",
                "days",
                "earliest_start",
                "latest_end",
            )
            safe["courses"] = [
                _keep(row, "course_code", "course_name", "section", "credits")
                for row in (alternative.get("courses") or [])
                if isinstance(row, dict)
            ]
            safe["meetings"] = [
                _keep(row, "course_code", "course_name", "section", "day", "start", "end")
                for row in (alternative.get("meetings") or [])
                if isinstance(row, dict)
            ]
            safe["unplaced_courses"] = [
                _keep(row, "course_code", "course_name", "reason_code", "reason")
                for row in (alternative.get("unplaced_courses") or [])
                if isinstance(row, dict)
            ]
            out["alternatives"].append(safe)
    unplaced = result.get("unplaced_courses")
    if isinstance(unplaced, list):
        out["unplaced_courses"] = [
            _keep(row, "course_code", "course_name", "reason_code", "reason")
            for row in unplaced
            if isinstance(row, dict)
        ]
    return out


def _project_my_timetable(result: dict[str, Any], _: RemoteIdentityMap) -> dict[str, Any]:
    """Instructor NAMES are dropped. A timetable answer needs the day, the time
    and the room; who teaches it is a member of staff whose name does not need to
    leave the institution to answer "when is my lecture"."""
    out = _envelope(result)
    out.update(_keep(result, "academic_year", "term"))
    meetings = result.get("meetings")
    if isinstance(meetings, list):
        out["meetings"] = [
            _keep(
                m,
                "course_code",
                "course_name",
                "section",
                "day",
                "start",
                "end",
                "start_time",
                "end_time",
                "room",
            )
            for m in meetings
            if isinstance(m, dict)
        ]
    return out


def _project_my_advisor(result: dict[str, Any], _: RemoteIdentityMap) -> dict[str, Any]:
    """The adviser's NAME, ID and EMAIL are all dropped.

    "Who is my adviser" is answered by the UI from the full local result. The
    model needs to know only WHETHER one is assigned, so that it can say so and
    point the student at the right screen — sending a member of staff's email
    address to an external provider to achieve that is not a trade worth making.
    """
    out = _envelope(result)
    out["advisor_assigned"] = bool(result.get("advisor_id") or result.get("advisor_name"))
    out.update(_keep(result, "department", "office_hours_available"))
    return out


def _policy_row(row: dict[str, Any], *, is_direct: bool) -> dict[str, Any]:
    """One policy, with the fields that govern how it may be USED.

    The first version of this projector sent the text and the citation and
    stopped, which is a worse failure than sending too much: without
    `decision_use`, the direct/background split and the conflict resolution, the
    model cannot tell explaining a rule from adjudicating a student's case, and
    the contract that separates those is the whole reason the store is
    structured.

    Internal PROSE does not go — `runtime_use_reason`, `runtime_use_note`,
    `never_infer`, `open_question`, `notes` are the store's guidance to us, and a
    model shown them will quote them at a student. Their MEANING survives as
    controlled enums and booleans.

    `authority.approved_by` and `approved_at` are dropped as well: an approver is
    a named member of staff.
    """
    conflicts = row.get("conflicts") if isinstance(row.get("conflicts"), list) else []
    projected: dict[str, Any] = {
        **_keep(row, "policy_id", "topic", "title_ar", "statement_ar", "rule", "exceptions"),
        # THE SAFETY STATE.
        "decision_use": row.get("decision_use"),
        "is_direct_evidence": is_direct,
        "requires_user_inputs": row.get("decision_use") == "PERMITTED_WITH_USER_PROVIDED_INPUTS",
        "student_case_evaluable": row.get("decision_use")
        in {"PARTIALLY_EVALUABLE", "PERMITTED_WITH_USER_PROVIDED_INPUTS"},
        "has_conflict": bool(conflicts),
        # `source_is_unclear_on` and `open_question` both mean "the source does
        # not settle this". The boolean carries that without the prose.
        "source_leaves_unresolved": bool(
            row.get("source_is_unclear_on") or row.get("open_question")
        ),
    }
    if isinstance(row.get("citation"), dict):
        projected["citation"] = _keep(
            row["citation"], "policy_id", "document_title", "edition", "page"
        )
    if isinstance(row.get("effective"), dict):
        projected["effective"] = _keep(
            row["effective"], "from", "to", "currentness_status", "expired"
        )
    if isinstance(row.get("authority"), dict):
        # Level and precedence decide which of two conflicting rules governs.
        # The approver's name and timestamp decide nothing the model needs.
        projected["authority"] = _keep(
            row["authority"], "level", "precedence_rank", "approval_status"
        )
    if conflicts:
        projected["conflicts"] = [
            _keep(
                c,
                "conflict_id",
                "subject",
                "this_policy_is",
                "governs",
                "resolution",
                "higher_authority_says",
                "caveat",
            )
            for c in conflicts
            if isinstance(c, dict)
        ]
    for field_name in ("concept_id", "governing_entity", "action", "claim_types", "role"):
        if field_name in row:
            projected[field_name] = row[field_name]
    return projected


def _project_policy_lookup(result: dict[str, Any], _: RemoteIdentityMap) -> dict[str, Any]:
    out = _envelope(result)
    out.update(
        _keep(
            result,
            "query",
            "strategy",
            "matched_topics",
            "policy_count",
            "total_matched",
            "truncated",
            "as_of",
            "note",
            "question_concepts",
            "grounding_state",
        )
    )
    # The classification IS the contract: a figure may come only from direct
    # evidence, so which bucket a record is in has to survive.
    for bucket, is_direct in (
        ("policies", False),
        ("direct_policy_evidence", True),
        ("background_policy_evidence", False),
        ("conflicting_policy_evidence", False),
        ("irrelevant_policy_evidence", False),
    ):
        rows = result.get(bucket)
        if isinstance(rows, list):
            out[bucket] = [_policy_row(r, is_direct=is_direct) for r in rows if isinstance(r, dict)]
    citable = result.get("citable")
    if isinstance(citable, list):
        out["citable"] = [
            _keep(c, "policy_id", "document_title", "edition", "page")
            for c in citable
            if isinstance(c, dict)
        ]
    out["answers_the_question"] = bool(result.get("direct_policy_evidence"))
    return out


def _project_get_student_context(
    result: dict[str, Any], identities: RemoteIdentityMap
) -> dict[str, Any]:
    """The whole student record, reduced to the academic facts an answer needs.

    Dropped: `student_id`, `name`, `advisor_id`, `status`, and the
    `regulatory_basis.authority`/`verification_status` and `qualification.*`
    blocks, which are operator-facing. Kept: programme, credit totals, course
    evidence, recommendations and the credit policy — including `phrasing_ar`,
    because that is the terminology contract the answer must honour.
    """
    out = _envelope(result)
    context = result.get("student_context")
    if not isinstance(context, dict):
        return out

    student = context.get("student") if isinstance(context.get("student"), dict) else {}
    # NO IDENTITY AND NO ALIAS. A student asking about their own record needs
    # neither: the server already knows who is asking, and `STUDENT_1` would be a
    # reference to the only person in the conversation. Aliases earn their keep
    # only where an adviser result contains several students to tell apart.
    projected_student = _keep(
        student,
        "program",
        "section",
        "gpa",
        "total_registered_credits",
        "total_earned_credits",
        "current_registered_credits",
    )

    evidence = (
        context.get("course_evidence") if isinstance(context.get("course_evidence"), dict) else {}
    )
    registrations = (
        evidence.get("current_term_registrations")
        if isinstance(evidence.get("current_term_registrations"), dict)
        else {}
    )
    projected_evidence: dict[str, Any] = {
        "passed": evidence.get("passed") if isinstance(evidence.get("passed"), list) else [],
        "studying": evidence.get("studying") if isinstance(evidence.get("studying"), list) else [],
        "remaining_requirement_count": evidence.get("remaining_requirement_count"),
        "remaining_requirements": _course_rows(
            evidence.get("remaining_requirements"),
            "course_code",
            "course_name",
            "type",
            "programme_term",
            "credit_hours",
        ),
        "programme_totals": evidence.get("programme_totals")
        if isinstance(evidence.get("programme_totals"), dict)
        else {},
        "current_term_registrations": {
            **_keep(
                registrations,
                "academic_year",
                "term",
                "source",
                "registered_course_count",
                "registered_credit_hours",
            ),
            "registrations": _course_rows(
                registrations.get("registrations"),
                "course_code",
                "course_name",
                "section",
                "credit_hours",
                "retake",
            ),
        },
    }

    policy = (
        context.get("recommendation_policy")
        if isinstance(context.get("recommendation_policy"), dict)
        else {}
    )
    projected_policy = _keep(
        policy,
        "max_recommended_credit_hours",
        "recommended_credit_hours",
        "credit_hours_unknown_for",
        "phrasing_ar",
        "regulatory_min_credit_hours",
        "regulatory_max_credit_hours",
        "regulatory_range_unknown",
        "regulatory_range_instruction",
        "note",
    )
    basis = policy.get("regulatory_basis")
    if isinstance(basis, dict):
        # The citation may be quoted; the authority and verification status are
        # our own review metadata.
        projected_policy["regulatory_basis"] = _keep(
            basis, "document", "page", "policy_id", "applies_to", "hedge"
        )

    out["student_context"] = {
        "mode": context.get("mode"),
        "student": projected_student,
        "term_context": _keep(
            context.get("term_context") if isinstance(context.get("term_context"), dict) else {},
            "academic_year",
            "term",
        ),
        "course_evidence": projected_evidence,
        "recommendations": _course_rows(
            context.get("recommendations"),
            "course_code",
            "course_name",
            "credit_hours",
            "prerequisites",
        ),
        "recommendation_policy": projected_policy,
        "limits": context.get("limits") if isinstance(context.get("limits"), dict) else {},
    }
    return out


def _project_find_students(result: dict[str, Any], identities: RemoteIdentityMap) -> dict[str, Any]:
    """Rows become `STUDENT_n` plus the attributes the model reasons over.

    Names and ids never leave. The full local result stays available to the
    adviser-facing evidence panel, which is authorised to show them.
    """
    out = _envelope(result)
    out.update(_keep(result, "count", "summary", "filters_used", "truncated"))
    rows = result.get("rows") or result.get("students")
    if isinstance(rows, list):
        out["rows"] = [
            {
                **(
                    {"student_ref": identities.reference_for(row["student_id"])}
                    if row.get("student_id") is not None
                    else {}
                ),
                **_keep(row, "program", "section", "status", "gpa", "level", "programme_term"),
            }
            for row in rows
            if isinstance(row, dict)
        ]
    return out


PROJECTORS = {
    "lookup_course": _project_lookup_course,
    "course_prerequisites": _project_course_prerequisites,
    "my_progress": _project_my_progress,
    "graduation_progress": _project_graduation_progress,
    "why_course_locked": _project_why_course_locked,
    "my_plan_by_term": _project_my_plan_by_term,
    "recommend_courses": _project_recommend_courses,
    "my_clash_free_sections": _project_my_clash_free_sections,
    "build_my_timetable": _project_build_my_timetable,
    "build_timetable_proposal": _project_build_timetable_proposal,
    "my_timetable": _project_my_timetable,
    "my_advisor": _project_my_advisor,
    "policy_lookup": _project_policy_lookup,
    "get_student_context": _project_get_student_context,
    "find_students": _project_find_students,
}


# ── the boundary ─────────────────────────────────────────────────


def project_tool_result_for_remote(
    tool_name: str, result: dict[str, Any], identities: RemoteIdentityMap
) -> dict[str, Any]:
    """The ONLY way a tool result reaches an external provider.

    Fails closed three ways: an unknown capability, a known capability with no
    projector, and a DENY capability all refuse rather than pass something
    through. The DENY refusal is generic — naming the field that made it
    sensitive would disclose the thing being protected.
    """
    exposure = REMOTE_POLICY.get(tool_name)
    if exposure is None:
        raise LLMPrivacyError(
            f"capability {tool_name!r} has no remote-exposure decision; "
            "refusing to send its result to an external provider."
        )
    if exposure is RemoteExposure.DENY:
        return dict(DENIED_RESULT)
    if not isinstance(result, dict):
        raise LLMPrivacyError(f"capability {tool_name!r} returned a non-dict result.")
    projector = PROJECTORS.get(tool_name)
    if projector is None:
        # Including ALLOW: "remotely usable" is not "send whatever it returns".
        raise LLMPrivacyError(
            f"capability {tool_name!r} is marked {exposure.value} but has no projector."
        )
    return projector(result, identities)


def project_verified_context_for_remote(
    context: dict[str, Any], identities: RemoteIdentityMap
) -> dict[str, Any]:
    """The seeded context, reduced the same way.

    The single-shot path serialises this whole object into the user message, so
    it is exactly as sensitive as a tool result and gets the same treatment. It
    reuses the `get_student_context` projector where the shapes coincide, so the
    two cannot drift apart.
    """
    if not isinstance(context, dict):
        raise LLMPrivacyError("verified_context is not a dict; refusing to serialise it.")

    projected: dict[str, Any] = {}
    # The context is the student_context shape one level up.
    wrapped = _project_get_student_context({"student_context": context}, identities)
    inner = wrapped.get("student_context")
    if isinstance(inner, dict):
        projected.update({k: v for k, v in inner.items() if v not in (None, {}, [])})

    # Tool results seeded by the deterministic planner get the per-tool treatment.
    tool_results = context.get("tool_results")
    if isinstance(tool_results, list):
        projected["tool_results"] = [
            project_tool_result_for_remote(str(entry.get("tool") or ""), entry, identities)
            for entry in tool_results
            if isinstance(entry, dict)
        ]

    for key in ("policy_evidence", "credit_policy_evidence"):
        evidence = context.get(key)
        if not isinstance(evidence, dict):
            continue
        # Projected ALREADY, when the caller collapsed the buckets for the prompt.
        # Running the projector a second time would re-derive directness from the
        # bucket a row now sits in and mark the governing records
        # `is_direct_evidence: false` — the projector is not idempotent about
        # classification, and it cannot be: the bucket IS the classification.
        already = (
            not ("direct_policy_evidence" in evidence or "background_policy_evidence" in evidence)
            and "policies" in evidence
        )
        projected[key] = dict(evidence) if already else _project_policy_lookup(evidence, identities)

    return projected


def _strip_identity(node: Any) -> Any:
    """Remove identity properties at EVERY depth, keeping the schema valid.

    Recursive because a parameter object is not always flat — `properties`,
    `items`, `$defs`, `anyOf`/`oneOf`/`allOf` all nest, and an identity field one
    level down is exactly as much of an invitation as one at the top.

    Three things must stay true afterwards or the provider rejects the schema and
    the tool silently stops working:
      * a removed property is also removed from `required`;
      * `additionalProperties` stays false, or the model may send the field back
        under a name the schema no longer mentions;
      * a `properties` block emptied by the strip does not become `{"required":
        ["student_id"]}` pointing at nothing.
    """
    if isinstance(node, list):
        return [_strip_identity(item) for item in node]
    if not isinstance(node, dict):
        return node

    out: dict[str, Any] = {}
    for key, value in node.items():
        if key == "properties" and isinstance(value, dict):
            out[key] = {
                name: _strip_identity(sub)
                for name, sub in value.items()
                if name not in IDENTITY_PARAMETERS
            }
        elif key == "required" and isinstance(value, list):
            out[key] = [r for r in value if r not in IDENTITY_PARAMETERS]
        else:
            out[key] = _strip_identity(value)

    if "properties" in out and isinstance(out["properties"], dict):
        # Closed by default: an open object lets the model post an identity the
        # schema no longer advertises, which is the hole this was meant to close.
        out.setdefault("additionalProperties", False)
        out["additionalProperties"] = False
    return out


def _validate_transformed_schema(schema: dict[str, Any]) -> None:
    """A transformed schema that is invalid is worse than one that is unsafe: the
    provider rejects the request and the capability quietly disappears."""
    function = schema.get("function")
    if not isinstance(function, dict) or not str(function.get("name") or "").strip():
        raise LLMPrivacyError("a transformed tool schema lost its function name.")
    parameters = function.get("parameters")
    if parameters is None:
        return
    if not isinstance(parameters, dict):
        raise LLMPrivacyError(f"{function['name']}: parameters is not an object after transform.")
    properties = parameters.get("properties")
    required = parameters.get("required") or []
    if properties is not None and not isinstance(properties, dict):
        raise LLMPrivacyError(f"{function['name']}: properties is not an object after transform.")
    dangling = [r for r in required if isinstance(properties, dict) and r not in properties]
    if dangling:
        raise LLMPrivacyError(
            f"{function['name']}: required names {dangling} that no longer exist after the strip."
        )
    for name in properties or {}:
        if name in IDENTITY_PARAMETERS:
            raise LLMPrivacyError(f"{function['name']}: identity parameter {name!r} survived.")


#: What an adviser capability gets INSTEAD of `student_id`. Removing the
#: parameter outright works for a student's own tools — the session identifies
#: them — but an adviser following up on a returned student has to be able to say
#: WHICH one, and the only safe way to say it is a reference we issued.
STUDENT_REF_PROPERTY = {
    "type": "string",
    "description": (
        "Opaque reference to a student returned earlier in THIS conversation, "
        "e.g. STUDENT_REF_A1B2_1. Real student numbers are not accepted."
    ),
}


def remote_tool_schemas(
    schemas: list[dict[str, Any]], *, allow_student_ref: bool = False
) -> list[dict[str, Any]]:
    """Drop DENY capabilities, strip identity parameters from the rest, validate.

    The result projection is not enough on its own. Eleven of the thirteen
    student-scope schemas advertise a `student_id` parameter, and a schema that
    names a real identity invites the model to supply one — inventing it, or
    echoing one it saw. The server resolves the student from the session, so the
    parameter has no purpose remotely beyond being a liability.
    """
    out: list[dict[str, Any]] = []
    for schema in schemas:
        function = schema.get("function") if isinstance(schema.get("function"), dict) else {}
        name = str(function.get("name") or "")
        if REMOTE_POLICY.get(name) is RemoteExposure.DENY:
            continue
        had_student_id = "student_id" in (
            ((function.get("parameters") or {}).get("properties") or {})
            if isinstance(function.get("parameters"), dict)
            else {}
        )
        transformed = _strip_identity(schema)
        if allow_student_ref and had_student_id:
            # Adviser mode: swap the real identifier for a reference. Note this
            # is a SUBSTITUTION, not merely a removal — a capability that can
            # only ever act on the session's own student is useless to an adviser
            # asking about somebody in their portfolio.
            params = transformed["function"].setdefault("parameters", {"type": "object"})
            params.setdefault("properties", {})["student_ref"] = dict(STUDENT_REF_PROPERTY)
            params["additionalProperties"] = False
        _validate_transformed_schema(transformed)
        out.append(transformed)
    return out


def reject_identity_arguments(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Refuse a forged identity argument, even though the schema never offered it.

    A schema is a description, not an enforcement: a model may send any JSON it
    likes. Silently dropping a forged `student_id` would be worse than refusing —
    the call would proceed against the session's student while the model believed
    it had asked about someone else, and the answer would be about the wrong
    person with no sign that anything went wrong.

    Returns the arguments unchanged so the execution order reads as a pipeline at
    the call site. It never edits them: an argument set that needed editing to be
    safe is one that should not run.
    """
    if not isinstance(arguments, dict):
        return arguments
    forged = sorted(set(arguments) & IDENTITY_PARAMETERS)  # `student_ref` is not one
    if forged:
        raise LLMPrivacyError(
            f"{tool_name}: {', '.join(forged)} may not be supplied on a remote backend; "
            "the student is resolved from the session."
        )
    return arguments


def resolve_reference_arguments(
    tool_name: str, arguments: dict[str, Any], identities: RemoteIdentityMap
) -> dict[str, Any]:
    """`student_ref` -> real `student_id`, or refuse.

    Runs BEFORE the authorisation check, not instead of it. Resolving a reference
    proves only that this answer issued it; whether the principal may still see
    that student is the registry's question, asked immediately afterwards. A
    reference is a way of naming a student without exposing them, never a
    permission to read them.
    """
    if not isinstance(arguments, dict) or "student_ref" not in arguments:
        return arguments
    resolved = dict(arguments)
    reference = resolved.pop("student_ref")
    resolved["student_id"] = identities.resolve(str(reference))
    return resolved


def remote_exposure_for(tool_name: str) -> RemoteExposure:
    """The decision recorded for this capability, or refuse.

    An unmapped capability is the case that matters: it is new, nobody has looked
    at what it returns, and the safe answer is the one that does not run it.
    """
    exposure = REMOTE_POLICY.get(tool_name)
    if exposure is None:
        raise LLMPrivacyError(
            f"capability {tool_name!r} has no remote-exposure decision; "
            "refusing to run it on a remote backend."
        )
    return exposure


def assert_remote_capability_allowed(
    tool_name: str, exposure: RemoteExposure | None = None
) -> None:
    """Executor-level refusal, as defence in depth — and BEFORE execution.

    `remote_tool_schemas` already withholds a DENY capability from the provider.
    This catches the case where a malformed or adversarial model response asks
    for one by name anyway — the schema is a description, not an enforcement.

    It also refuses a capability with no projector. That is a programming error
    rather than an attack, but it is detectable here, before a database read
    happens whose result could then only be thrown away. Failing at the exposure
    check keeps "we cannot send this" from becoming "we read it anyway".
    """
    exposure = exposure or remote_exposure_for(tool_name)
    if exposure is RemoteExposure.DENY:
        raise LLMPrivacyError(f"capability {tool_name!r} is not available on a remote backend.")
    if PROJECTORS.get(tool_name) is None:
        raise LLMPrivacyError(
            f"capability {tool_name!r} is marked {exposure.value} but has no projector."
        )


def authorise_resolved_arguments(
    tool_name: str, arguments: dict[str, Any], scope: dict[str, Any] | None
) -> None:
    """Re-check scope on the RESOLVED student, immediately before execution.

    The question preflight authorised the id the student wrote. This authorises
    the id the model is about to act on — a different fact, arrived at through a
    reference the model chose, several turns later. Between the two, scope can
    change: a portfolio is reassigned, an account is deactivated, a departmental
    scope is edited. Treating the preflight as sufficient would mean the window
    between them is a window in which a stale reference still works.

    It also removes an ordering hazard. `resolve_reference_arguments` turns a
    reference into a real id, and a reader who sees that line can reasonably
    assume resolution implies permission. It does not: resolution proves only
    that THIS answer issued the reference.

    Delegates to `_resolve_scoped_student_id`, the capability layer's own
    resolver, so remote mode cannot drift into a second opinion about who may
    read whom.
    """
    if not isinstance(arguments, dict) or arguments.get("student_id") in (None, ""):
        return
    from core.services.virtual_advisor_capabilities import _resolve_scoped_student_id

    try:
        requested = int(arguments["student_id"])
    except (TypeError, ValueError) as exc:
        raise LLMPrivacyError(f"{tool_name}: the resolved student id is not usable.") from exc
    allowed, error = _resolve_scoped_student_id({"student_id": requested}, scope)
    if error or allowed != requested:
        # Deliberately generic, and deliberately identical to the refusal for a
        # student who does not exist. `_resolve_scoped_student_id` distinguishes
        # "not found" from "outside your portfolio"; repeating that distinction
        # to a model — and into a provider transcript — turns the boundary into a
        # directory oracle that answers "is 4502157 a real student?" for free.
        raise LLMPrivacyError(f"{tool_name}: this request may not read that student.")


# ── free text: the question, and the conversation history ────────
#
# Projecting structured results is not enough. A student writes "رقمي 4502156",
# an adviser searches by a real id, and a stored history carries both back into
# every subsequent turn. None of that is in a field called `student_id`.

#: Western and Arabic-Indic digits. A student typing ٤٥٠٢١٥٦ has written their
#: id just as surely as one typing 4502156, and a pattern that only knows ASCII
#: would pass it straight through.
_DIGITS = "0-9\u0660-\u0669\u06f0-\u06f9"

#: 6-9 digit runs are CANDIDATES, not identities. The project's existing
#: high-precision rule (`virtual_advisor._STUDENT_ID_RE`) is what makes the
#: shorter numbers safe: a course code is `AI221`, a year `1448`, a load `19`, a
#: time `09:00`, a page `28`. None reaches six digits.
_ID_CANDIDATE = re.compile(rf"(?<![{_DIGITS}])[{_DIGITS}]{{6,9}}(?![{_DIGITS}])")

#: Any email address is personal — a student's, a colleague's, or an invented
#: one. Blanket redaction also removes official service contacts; that is
#: deliberate remote-mode minimisation rather than an oversight.
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")

#: Saudi mobile numbers, local or international, with or without separators.
_PHONE = re.compile(r"(?:(?:\+|00)966|0)\s?5\d(?:[\s-]?\d){7}\b")

#: Anything SHAPED like one of our references. Used to detect forgery, not to
#: redact: a reference we did not issue must never reach the resolver. The nonce
#: run is bounded generously rather than at the exact nonce length — a forgery is
#: unlikely to guess the length either, and a pattern that only matches
#: correctly-sized references would wave the short ones straight through.
_ALIAS_SHAPE = re.compile(
    r"(?<![A-Za-z0-9])STUDENT(?:_REF)?(?:_[A-Za-z0-9-]{1,64})?_\d+(?![A-Za-z0-9])",
    re.IGNORECASE,
)

EMAIL_PLACEHOLDER = "[EMAIL_REDACTED]"
PHONE_PLACEHOLDER = "[PHONE_REDACTED]"
NAME_PLACEHOLDER = "[NAME_REDACTED]"
#: Only ever appears in text the MODEL wrote. See `sanitise_model_text_for_remote`.
UNVERIFIED_ID_PLACEHOLDER = "[IDENTIFIER_REMOVED]"

_ARABIC_INDIC = {ord(c): str(i % 10) for i, c in enumerate("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹")}


def _as_western(digits: str) -> str:
    return digits.translate(_ARABIC_INDIC)


def fold_digits(text: str) -> str:
    """Arabic-Indic and Extended Arabic-Indic digits to Western.

    Public because the OUTPUT contract needs the same folding the transport
    boundary uses. A second table somewhere else drifts: this one already covers
    both Unicode ranges, and the checker that misses `U+06F0..U+06F9` is the one
    an Arabic answer walks straight past.
    """
    return str(text or "").translate(_ARABIC_INDIC)


def reference_tokens_in(text: str) -> list[str]:
    """Every token SHAPED like a student reference, issued or not.

    Public for the same reason as `fold_digits`. The output gate must ask "did
    this answer name a reference nobody issued", and it must ask with the exact
    pattern that mints them — a second pattern is how the forgery detector and
    the reference format got out of step once already.
    """
    return _ALIAS_SHAPE.findall(str(text or ""))


def sanitise_text_for_remote(
    text: str,
    identities: RemoteIdentityMap,
    *,
    known_names: tuple[str, ...] = (),
    authorise_id: Callable[[int], bool] | None = None,
    redact_unverified: bool = False,
) -> str:
    """Free text, with verified identifiers aliased and unverifiable ones refused.

    THREE OUTCOMES, and the middle one is the correction that matters:

      * a candidate `authorise_id` confirms  -> `STUDENT_REF_<nonce>_n`
      * a candidate it cannot confirm        -> `UnverifiedIdentity`, no request
      * everything else                      -> untouched

    An earlier version aliased every 6-9 digit run. That is wrong in a way that
    is worse than leaking: «رقم المعاملة 12345678» becomes a student reference,
    the model is told a transaction number is a person, and the map will happily
    resolve it later. A number nobody can vouch for does not become an identity
    by being the right length.

    `authorise_id` is supplied by the caller because only the caller has scope.
    In student mode it accepts exactly the authenticated id and nothing else; in
    adviser mode it checks existence AND that this principal may see that
    student, before any alias exists.

    Names are replaced from an EXACT known set — the principal's stored name,
    names returned by an authorised lookup, configured sentinels. No proper-name
    pattern: one would mangle course titles, policy names and ordinary prose to
    protect nothing.
    """
    if not text:
        return text
    source = str(text)

    # Forged references first, before anything can be mistaken for one.
    for candidate in _ALIAS_SHAPE.findall(source):
        if not identities.issued(candidate):
            raise LLMPrivacyError(
                "the text contains something shaped like a student reference that "
                "this answer did not issue; refusing to send it."
            )

    out = _EMAIL.sub(EMAIL_PLACEHOLDER, source)
    out = _PHONE.sub(PHONE_PLACEHOLDER, out)
    for name in known_names:
        cleaned = str(name or "").strip()
        if len(cleaned) >= 3:
            out = re.sub(re.escape(cleaned), NAME_PLACEHOLDER, out, flags=re.IGNORECASE)

    return _replace_candidates(out, identities, authorise_id, redact_unverified=redact_unverified)


def _replace_candidates(
    text: str,
    identities: RemoteIdentityMap,
    authorise_id: Callable[[int], bool] | None,
    *,
    redact_unverified: bool,
) -> str:
    def replace(match: re.Match[str]) -> str:
        raw = match.group(0)
        try:
            candidate = int(_as_western(raw))
        except ValueError as exc:  # pragma: no cover - the pattern matches digits only
            raise UnverifiedIdentity(
                "a numeric identifier could not be read; refusing to send the text."
            ) from exc
        if authorise_id is None or not authorise_id(candidate):
            if redact_unverified:
                return UNVERIFIED_ID_PLACEHOLDER
            raise UnverifiedIdentity(
                "the text contains a 6-9 digit identifier that is not a student this "
                "request may access; refusing to send it to an external provider."
            )
        return identities.reference_for(candidate)

    return _ID_CANDIDATE.sub(replace, text)


def sanitise_model_text_for_remote(
    text: str,
    identities: RemoteIdentityMap,
    *,
    known_names: tuple[str, ...] = (),
    authorise_id: Callable[[int], bool] | None = None,
) -> str:
    """Text the MODEL wrote, on its way back to the provider that wrote it.

    Same rules, one different outcome: an unverifiable candidate is REDACTED
    rather than refused.

    The distinction is about who is responsible for the number, not about how
    dangerous it is.

      * A user writing an id nobody can vouch for is asking about a person this
        request has no authority over. Refusing is the answer, and the request
        stops — that decision is not softened here.

      * A model writing one has invented it, and the retry that fixes exactly
        that is the thing being sent. Refusing would leave the invented
        identifier in the answer the student actually receives: the safety check
        would fail closed on the transport and open on the output, which is the
        wrong way round.

    Sending it back is also not a disclosure — the provider generated it one turn
    ago. It is removed anyway, because a fabricated id can collide with a real
    student and because repeating it invites the model to keep it.
    """
    return sanitise_text_for_remote(
        text,
        identities,
        known_names=known_names,
        authorise_id=authorise_id,
        redact_unverified=True,
    )


def sanitise_messages_for_remote(
    messages: list[dict[str, Any]],
    identities: RemoteIdentityMap,
    *,
    known_names: tuple[str, ...] = (),
    authorise_id: Callable[[int], bool] | None = None,
) -> list[dict[str, Any]]:
    """Every message that will be serialised, including ones already sent.

    History is the easy thing to forget: a question sanitised on turn one is
    replayed verbatim on turn two by whatever assembled the conversation, and a
    retry prompt quotes the model's previous answer back at it. This runs over
    the whole list each time rather than trusting an earlier pass.

    IDEMPOTENT: a second pass sees `[EMAIL_REDACTED]` and `STUDENT_REF_…` — the
    placeholders match no pattern, and the reference is one this map issued, so
    nothing is double-processed.

    Tool messages are NOT touched: their content is already a projected result,
    and re-scanning a projection would corrupt legitimate structured values.

    THE ROLE DECIDES WHAT AN UNVERIFIABLE NUMBER MEANS. An `assistant` message is
    something the model wrote, so an id in it was invented and is redacted; a
    `user` message is something a person wrote, so an id in it is a request to
    act on somebody, and that refuses. See `sanitise_model_text_for_remote`.

    One consequence worth naming: a conversation that began on the local backend
    can hold a stored question containing a real id, and continuing it remotely
    refuses. That is the intended direction. The question could not have been
    asked remotely in the first place, and quietly redacting it would let a
    backend switch relax a rule the same text was refused under an hour earlier.
    """
    out: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        if message.get("role") == "tool":
            out.append(message)
            continue
        content = message.get("content")
        out.append(
            {
                **message,
                "content": sanitise_text_for_remote(
                    content,
                    identities,
                    known_names=known_names,
                    authorise_id=authorise_id,
                    redact_unverified=message.get("role") == "assistant",
                ),
            }
            if isinstance(content, str)
            else message
        )
    return out


def student_mode_authoriser(authenticated_student_id: int | str) -> Callable[[int], bool]:
    """Student mode: exactly one id is verifiable, and it is the caller's own.

    A student's question mentioning any other 6-9 digit number is refused rather
    than aliased — they have no authorised route to another student's record, so
    a reference to one should not exist even opaquely.

    No database read: the answer is a comparison against the session's own id, so
    a student cannot use their own question as a probe for whether some number is
    a real student. The refusal for "another student" and for "a courier tracking
    number" is one refusal, arrived at without looking anything up.
    """
    try:
        own = int(str(authenticated_student_id).strip())
    except (TypeError, ValueError):
        return lambda _candidate: False
    return lambda candidate: candidate == own


def adviser_mode_authoriser(scope: dict[str, Any] | None) -> Callable[[int], bool]:
    """Adviser mode: resolve the candidate locally, then authorise it, then alias.

    An adviser legitimately writes a student's id — that is how the job is done —
    so refusing every number would make the remote backend useless to staff. The
    number still may not become a reference on the strength of its LENGTH. It is
    resolved against the roster and checked against this principal's scope first,
    entirely locally, and only a candidate that survives both is aliased.

    NONEXISTENT AND INACCESSIBLE ARE THE SAME OUTCOME, and that is the point.
    `_resolve_scoped_student_id` knows the difference and says so, which is right
    for an adviser-facing error and wrong here: two distinguishable refusals turn
    a question into a lookup, and an adviser could enumerate the roster outside
    their portfolio one question at a time without ever seeing a record. Both
    return False, one refusal is raised by the caller, and no provider request is
    made either way.

    Memoised per request because the same id recurs across the question and every
    history message. A cache also keeps the answer stable inside one request: an
    id that is authorised once cannot be refused three messages later because a
    row changed mid-answer, which would produce a half-aliased conversation.
    """
    from core.services.virtual_advisor_capabilities import _resolve_scoped_student_id

    decided: dict[int, bool] = {}

    def authorise(candidate: int) -> bool:
        if candidate not in decided:
            try:
                allowed, error = _resolve_scoped_student_id({"student_id": candidate}, scope)
            except Exception:  # pragma: no cover - a resolver failure is a refusal
                logger.exception("Identity authorisation failed; refusing the candidate.")
                allowed, error = None, "unavailable"
            decided[candidate] = not error and allowed == candidate
        return decided[candidate]

    return authorise


def authoriser_for_scope(scope: dict[str, Any] | None) -> Callable[[int], bool]:
    """Pick the authoriser the principal's role earns.

    One dispatcher rather than a choice at each call site: "which authoriser does
    this caller get" is exactly the decision that goes wrong quietly, and getting
    it wrong in the permissive direction hands a student the adviser's resolver
    and a roster probe with it.
    """
    from core.services.rbac import ROLE_STUDENT

    scope = scope or {}
    if str(scope.get("role") or "") == ROLE_STUDENT:
        return student_mode_authoriser(scope.get("student_id"))
    return adviser_mode_authoriser(scope)


__all__ = [
    "DENIED_RESULT",
    "IDENTITY_PARAMETERS",
    "PROJECTORS",
    "REFUSED_RESULT",
    "REMOTE_POLICY",
    "STUDENT_REF_PROPERTY",
    "RemoteExposure",
    "RemoteIdentityMap",
    "UnverifiedIdentity",
    "adviser_mode_authoriser",
    "assert_remote_capability_allowed",
    "authorise_resolved_arguments",
    "authoriser_for_scope",
    "fold_digits",
    "project_tool_result_for_remote",
    "project_verified_context_for_remote",
    "reference_tokens_in",
    "reject_identity_arguments",
    "remote_exposure_for",
    "remote_tool_schemas",
    "resolve_reference_arguments",
    "sanitise_messages_for_remote",
    "sanitise_model_text_for_remote",
    "sanitise_text_for_remote",
    "student_mode_authoriser",
]
