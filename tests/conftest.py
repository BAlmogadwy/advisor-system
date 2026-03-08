import pytest

from core.services import rbac


@pytest.fixture(autouse=True)
def _reset_rbac_flags() -> None:  # noqa: PT004
    """Reset module-level flags so ensure_role_groups() re-creates groups after
    each test's transaction rollback."""
    rbac._groups_ensured = False
    rbac._scope_schema_ensured = False
