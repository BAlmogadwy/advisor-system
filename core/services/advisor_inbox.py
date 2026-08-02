"""Which cases a member of staff may see, and what they may do to them.

Staff-only is not authorisation. The student side encodes ownership in every
query, and the adviser side has to encode PERMITTED SCOPE the same way: a case is
visible on one of three grounds and no others — it is assigned to you, it belongs
to a student in a department you cover, or you administer escalations. Anything
else is a colleague's correspondence with somebody else's student.

One deliberate asymmetry with the evidence snapshot. That snapshot is frozen,
because a case must show the evidence the student was actually given. The QUEUE is
not: departmental scope is evaluated against the student's CURRENT programme, so a
student who transfers moves to the department that now advises them rather than
staying in the queue of the one that no longer does. Frozen evidence, live routing.
"""

from __future__ import annotations

from typing import Any

from django.db.models import QuerySet

from core.models import AdvisorEscalation, Student
from core.services.rbac import (
    ROLE_GENERAL_ADVISOR,
    ROLE_STUDENT,
    ROLE_SUPER_ADMIN,
    get_user_scope,
)


class InboxError(Exception):
    """The action is not one this member of staff may take on this case."""


#: Who may see everything. Kept explicit rather than "is_staff", so widening it is
#: an edit to this line and not a side effect of somebody being given a login.
ESCALATION_ADMIN_ROLES = frozenset({ROLE_SUPER_ADMIN})


def visible_cases(user: Any) -> QuerySet[AdvisorEscalation]:
    """The cases this user may open — as a QUERYSET, not a post-filter.

    Returning everything and checking each row afterwards is one forgotten line
    away from a leak, and the forgotten line looks like working code. Scope is part
    of the filter here for the same reason it is on the student side.
    """
    scope = get_user_scope(user)
    role = str(scope.get("role") or "")
    if not role or role == ROLE_STUDENT:
        return AdvisorEscalation.objects.none()

    if role in ESCALATION_ADMIN_ROLES:
        return AdvisorEscalation.objects.all()

    advisor_id = str(scope.get("advisor_id") or "").strip()
    departments = [
        str(d).strip().upper() for d in (scope.get("departments") or []) if str(d).strip()
    ]

    from django.db.models import Q

    grounds = Q(pk__in=[])  # nothing, until a ground is established
    if advisor_id:
        grounds |= Q(assigned_adviser_id=advisor_id)
        # An adviser's own students, whether or not the case is assigned yet —
        # otherwise nobody can pick up a new case except an administrator.
        grounds |= Q(
            student_id__in=Student.objects.filter(advisor_id=advisor_id).values("student_id")
        )
    if role == ROLE_GENERAL_ADVISOR and departments:
        grounds |= Q(
            student_id__in=Student.objects.filter(program__in=departments).values("student_id")
        )
    return AdvisorEscalation.objects.filter(grounds)


def may_act_on(user: Any, case: AdvisorEscalation) -> bool:
    """Whether this user may CHANGE the case, not merely read it."""
    return visible_cases(user).filter(pk=case.pk).exists()


def adviser_label(user: Any) -> str:
    """How an action is attributed in the audit trail."""
    scope = get_user_scope(user)
    advisor_id = str(scope.get("advisor_id") or "").strip()
    name = (getattr(user, "get_full_name", lambda: "")() or "").strip()
    return name or advisor_id or getattr(user, "username", "") or "?"


# ── the transitions a case may actually make ─────────────────────

S = AdvisorEscalation.Status

#: Deliberately not a free-for-all. A case that can jump straight from OPEN to
#: CLOSED is one nobody has to say they looked at, and RESOLVED means a student
#: was answered — which is why it requires a response to exist.
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    S.OPEN: frozenset({S.ASSIGNED, S.NEEDS_INFORMATION, S.CLOSED}),
    S.ASSIGNED: frozenset({S.NEEDS_INFORMATION, S.RESOLVED, S.CLOSED}),
    S.NEEDS_INFORMATION: frozenset({S.ASSIGNED, S.RESOLVED, S.CLOSED}),
    S.RESOLVED: frozenset({S.CLOSED}),
    S.CLOSED: frozenset(),
}


def may_transition(case: AdvisorEscalation, to_status: str) -> bool:
    return to_status in ALLOWED_TRANSITIONS.get(case.status, frozenset())


def check_transition(case: AdvisorEscalation, to_status: str) -> None:
    """Raise unless the move is one this case may make.

    RESOLVED carries an extra condition: a case cannot be marked as having been
    dealt with while the student has been told nothing. That is the difference
    between resolving a case and closing it.
    """
    if to_status not in dict(S.choices):
        raise InboxError(f"{to_status} is not a case status.")
    if not may_transition(case, to_status):
        raise InboxError(f"A {case.status} case cannot become {to_status}.")
    if to_status == S.RESOLVED and not case.resolution_message.strip():
        raise InboxError("Record a reply to the student before resolving the case.")
