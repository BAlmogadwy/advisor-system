from datetime import UTC, datetime
from typing import Any

from django.contrib.auth.models import Group

from core.models import UserScope

ROLE_SUPER_ADMIN = "SUPER_ADMIN"
ROLE_GENERAL_ADVISOR = "GENERAL_ACADEMIC_ADVISOR"
ROLE_ADVISOR = "ADVISOR"
ROLE_STUDENT = "STUDENT"

ROLE_NAMES = [ROLE_SUPER_ADMIN, ROLE_GENERAL_ADVISOR, ROLE_ADVISOR]  # staff roles (validation)
# Every auth Group we seed, including the non-staff STUDENT role. STUDENT is kept
# OUT of ROLE_NAMES so the staff admin UI cannot mint students — they are provisioned
# only by the OTP login flow.
_ALL_ROLE_GROUPS = [*ROLE_NAMES, ROLE_STUDENT]

_groups_ensured = False


def ensure_role_groups() -> None:
    global _groups_ensured
    if _groups_ensured:
        return
    for name in _ALL_ROLE_GROUPS:
        Group.objects.get_or_create(name=name)
    _groups_ensured = True


_scope_schema_ensured = False


def ensure_scope_schema() -> None:
    global _scope_schema_ensured
    if _scope_schema_ensured:
        return
    # Schema is managed by Django migrations.
    # Keep this function as a compatibility no-op for existing call sites.
    _scope_schema_ensured = True
    return


def get_user_role(user: Any) -> str:
    if user.is_superuser:
        return ROLE_SUPER_ADMIN
    group_names = set(user.groups.values_list("name", flat=True))
    if ROLE_SUPER_ADMIN in group_names:
        return ROLE_SUPER_ADMIN
    if ROLE_GENERAL_ADVISOR in group_names:
        return ROLE_GENERAL_ADVISOR
    # A student is in the STUDENT group and must NEVER fall through to the ADVISOR
    # default below — that fall-through would hand them advisor-tier access.
    if ROLE_STUDENT in group_names:
        return ROLE_STUDENT
    return ROLE_ADVISOR


def get_user_scope(user: Any) -> dict[str, Any]:
    scope = UserScope.objects.filter(user_id=user.id).first()

    advisor_id = str(scope.advisor_id).strip() if scope and scope.advisor_id else ""
    deps_text = str(scope.departments).strip() if scope and scope.departments else ""
    departments = [x.strip().upper() for x in deps_text.replace(";", ",").split(",") if x.strip()]

    role = get_user_role(user)
    # A student's identity is the IMMUTABLE student_id persisted on UserScope at
    # provisioning time — never the mutable username (which a user can self-change).
    # Read only for STUDENT-role users so an advisor scope never carries a student_id.
    student_id = scope.student_id if (role == ROLE_STUDENT and scope and scope.student_id) else None

    return {
        "role": role,
        "advisor_id": advisor_id,
        "departments": departments,
        "student_id": student_id,
    }


def set_user_scope(
    user_id: int, advisor_id: str = "", departments: str = "", student_id: int | None = None
) -> None:
    UserScope.objects.update_or_create(
        user_id=user_id,
        defaults={
            "advisor_id": advisor_id.strip(),
            "departments": departments.strip(),
            "student_id": student_id,
            "updated_at": datetime.now(UTC).isoformat(),
        },
    )
