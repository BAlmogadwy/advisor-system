"""Migration 0072 adds rooms.exam_capacity and starts it at each room's capacity."""

from __future__ import annotations

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

BEFORE = [("core", "0071_exam_job_panel_fields")]
AFTER = [("core", "0072_room_exam_capacity")]


@pytest.mark.django_db(transaction=True)
def test_exam_capacity_starts_at_each_rooms_capacity():
    executor = MigrationExecutor(connection)
    executor.migrate(BEFORE)
    old_room = executor.loader.project_state(BEFORE).apps.get_model("core", "Room")
    old_room.objects.create(room_code="R-45", section="M", capacity=45)
    old_room.objects.create(room_code="R-45", section="F", capacity=30)
    old_room.objects.create(room_code="R-0", section="M", capacity=0)

    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(AFTER)
    new_room = executor.loader.project_state(AFTER).apps.get_model("core", "Room")

    rows = {
        (room.room_code, room.section): (room.capacity, room.exam_capacity)
        for room in new_room.objects.all()
    }
    assert rows == {
        ("R-45", "M"): (45, 45),
        ("R-45", "F"): (30, 30),
        ("R-0", "M"): (0, 0),
    }

    # Leave the database at the latest migration for the tests that follow.
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(executor.loader.graph.leaf_nodes())
