"""Who is asking the adviser, and about whom, as one object.

The adviser had two partially connected identity channels. `answer_virtual_advisor`
took a `student_id` parameter AND a `scope` dict that also carried a student id,
and the conversation view passed only the second — so `build_verified_student_context`
received `None` and returned its general-mode stub. Every stored turn was answered
with none of the student's own record in the prompt, and with an `available_tools`
list advertising cohort search into a student session. Nothing was exposed, because
two unrelated guards happened to hold; but the call did not say who was asking, and
safety resting on guards downstream of an unstated identity is safety by accident.

One object fixes the class of bug rather than the instance. A principal is built
from the authenticated request and from nothing else, is frozen, and is the only
thing passed onward — so there is no second channel to disagree with, and no call
site that can fill one in and forget the other.

**Identity comes from `UserScope`, never the session.** `UserScope.student_id` is
written once at provisioning (`student_otp.provision_student_user`) inside the same
transaction as the user row, and is what the RBAC layer already treats as
authoritative — deliberately not the username, which a user can change. The only
student id anywhere in `request.session` is `otp_student_id`: a PRE-authentication
claim set to whatever digits were posted, and popped the moment login succeeds.
Reading the session for identity would invent a channel that does not exist.

And never from the payload: a request that names a student id is describing what it
wants, not who it is.

**Asking and being asked about are different.** A student's principal is their own
record. A staff principal has a *subject* — the student being looked up — which is
authorised separately by `require_student_scope`. Both are the same class, because
both answer the one question `answer_virtual_advisor` needs: whose record loads,
and under whose authority.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from django.http import HttpRequest

from .rbac import ROLE_STUDENT, get_user_scope


class IdentityError(Exception):
    """The request carries no usable adviser identity.

    Raised rather than defaulted, because every default here is a
    reduced-privilege answer that LOOKS like a real one: a student whose id failed
    to resolve would get a generic reply about university regulations with nothing
    to say it was not about them.
    """


@dataclass(frozen=True)
class AdvisorPrincipal:
    """The authenticated caller. Frozen: identity is not something a request
    handler should be able to adjust as it goes."""

    role: str
    #: Whose record to load. The caller's own for a student; the looked-up subject
    #: for staff; None when staff ask a question about nobody in particular.
    student_id: int | None
    advisor_id: str = ""
    departments: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        """A student principal must name a student.

        The fail-closed logic used to live only in the two constructors, so
        `AdvisorPrincipal(role=ROLE_STUDENT, student_id=None)` was legal — and it
        rebuilds this module's entire reason for existing: `as_scope()` returns a
        student role with no id, `build_verified_student_context` returns its
        general-mode stub, and the answer is a plausible reply about university
        regulations that is not about anyone. Direct construction is a real pattern
        (the eval battery does it), so the invariant belongs on the type.
        """
        if self.role == ROLE_STUDENT and not (
            isinstance(self.student_id, int) and self.student_id > 0
        ):
            raise IdentityError("A student principal must carry a usable student id.")

    @classmethod
    def for_student(cls, request: HttpRequest) -> AdvisorPrincipal:
        """The signed-in student, asking about themselves.

        Fails closed on every branch: not a student, no scope row, or an
        unparseable id all raise. There is no "carry on with less" outcome.
        """
        scope = get_user_scope(getattr(request, "user", None))
        if str(scope.get("role", "")) != ROLE_STUDENT:
            raise IdentityError("This endpoint is for signed-in students.")
        return cls(role=ROLE_STUDENT, student_id=_student_id(scope.get("student_id")))

    @classmethod
    def for_staff(cls, request: HttpRequest, subject_student_id: Any = None) -> AdvisorPrincipal:
        """A staff member, optionally asking about one student.

        The subject is NOT authorisation — `require_student_scope` decides whether
        this member of staff may see that student, and must already have run.
        """
        scope = get_user_scope(getattr(request, "user", None))
        role = str(scope.get("role", ""))
        if not role:
            raise IdentityError("This request carries no role.")
        if role == ROLE_STUDENT:
            # A student reaching a staff entry point is a routing mistake, and
            # `for_student` is the constructor that clamps them to themselves.
            raise IdentityError("Students must use the student adviser.")
        subject = None if subject_student_id in (None, "") else _student_id(subject_student_id)
        return cls(
            role=role,
            student_id=subject,
            advisor_id=str(scope.get("advisor_id") or ""),
            departments=tuple(scope.get("departments") or ()),
        )

    def as_scope(self) -> dict[str, Any]:
        """The scope dict the capability layer expects.

        Derived from this object rather than assembled beside it, so the scope and
        the record being loaded cannot describe two different people. A student's
        scope carries their id — that is what clamps every capability to their own
        row. Staff scope carries none: the subject is an argument they must be
        authorised for, not an identity they hold.
        """
        if self.role == ROLE_STUDENT:
            return {"role": self.role, "student_id": self.student_id}
        return {
            "role": self.role,
            "advisor_id": self.advisor_id,
            "departments": list(self.departments),
            "student_id": None,
        }


def _student_id(raw: Any) -> int:
    """Parse a student id, or refuse.

    Non-positive values are refused rather than clamped. The WhatsApp gateway
    signals "no student" with -1, and a clamp to a minimum of 1 turned that denial
    into student number 1.
    """
    if raw in (None, ""):
        raise IdentityError("No student identity is linked to this account.")
    try:
        student_id = int(raw)
    except (TypeError, ValueError) as exc:
        raise IdentityError("The linked student identity is not usable.") from exc
    if student_id <= 0:
        raise IdentityError("The linked student identity is not usable.")
    return student_id
