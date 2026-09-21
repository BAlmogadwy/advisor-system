"""Student Advisor V2.1: semantic evidence planning over the V2 safety runtime.

V2.1 is intentionally a strategy, not a fork of the adviser.  It replaces the
legacy question-pattern routing with one schema-constrained semantic plan and
then reuses V2's read-only capability executor, identity/privacy boundary,
evidence validator, deterministic presentations, and fail-closed fallbacks.

The rollout flag is an explicit kill switch.  A failed V2.1 plan never falls
through to the regex-routed runtime for the same turn.  The dispatcher requires
V2 to remain enabled while V2.1 is enabled, so disabling only the V2.1 flag has
one defined rollback target.
"""

from __future__ import annotations

from typing import Any, cast

from django.conf import settings


def is_enabled() -> bool:
    """Return whether the semantic-planning strategy is selected."""

    return bool(getattr(settings, "STUDENT_ADVISOR_V21_ENABLED", False))


def answer_student_advisor_v21(**kwargs: Any) -> dict[str, Any]:
    """Run V2's hardened engine with legacy regex capability routing disabled."""

    from core.services.student_advisor_v2 import answer_student_advisor_v2

    kwargs.pop("_semantic_planning", None)
    return cast(
        dict[str, Any],
        answer_student_advisor_v2(**kwargs, _semantic_planning=True),
    )


__all__ = ["answer_student_advisor_v21", "is_enabled"]
