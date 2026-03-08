from datetime import UTC, datetime
from typing import Any

from django.contrib.auth.models import Group

from core.models import UserScope

ROLE_SUPER_ADMIN = "SUPER_ADMIN"
ROLE_GENERAL_ADVISOR = "GENERAL_ACADEMIC_ADVISOR"
ROLE_ADVISOR = "ADVISOR"

ROLE_NAMES = [ROLE_SUPER_ADMIN, ROLE_GENERAL_ADVISOR, ROLE_ADVISOR]

_groups_ensured = False


def ensure_role_groups() -> None:
    global _groups_ensured
    if _groups_ensured:
        return
    for name in ROLE_NAMES:
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
    if user.groups.filter(name=ROLE_SUPER_ADMIN).exists():
        return ROLE_SUPER_ADMIN
    if user.groups.filter(name=ROLE_GENERAL_ADVISOR).exists():
        return ROLE_GENERAL_ADVISOR
    return ROLE_ADVISOR


def get_user_scope(user: Any) -> dict[str, Any]:
    scope = UserScope.objects.filter(user_id=user.id).first()

    advisor_id = str(scope.advisor_id).strip() if scope and scope.advisor_id else ""
    deps_text = str(scope.departments).strip() if scope and scope.departments else ""
    departments = [x.strip().upper() for x in deps_text.replace(";", ",").split(",") if x.strip()]

    return {
        "role": get_user_role(user),
        "advisor_id": advisor_id,
        "departments": departments,
    }


def set_user_scope(user_id: int, advisor_id: str = "", departments: str = "") -> None:
    UserScope.objects.update_or_create(
        user_id=user_id,
        defaults={
            "advisor_id": advisor_id.strip(),
            "departments": departments.strip(),
            "updated_at": datetime.now(UTC).isoformat(),
        },
    )
