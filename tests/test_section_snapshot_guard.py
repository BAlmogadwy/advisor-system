from core.services.section_snapshot_guard import section_snapshot_operation_guard


def test_section_snapshot_operation_guard_is_non_reentrant() -> None:
    with section_snapshot_operation_guard(blocking=False) as first_acquired:
        assert first_acquired is True

        with section_snapshot_operation_guard(blocking=False) as second_acquired:
            assert second_acquired is False

    with section_snapshot_operation_guard(blocking=False) as acquired_after_release:
        assert acquired_after_release is True
