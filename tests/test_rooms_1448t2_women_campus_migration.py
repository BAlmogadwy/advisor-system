"""Migration 0073 applies the 1448 T2 women's-campus room sheet to the rooms table only."""

from __future__ import annotations

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

BEFORE = [("core", "0072_room_exam_capacity")]
AFTER = [("core", "0073_rooms_1448t2_women_campus")]


def _migrate(target):
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(target)
    return executor.loader.project_state(target).apps.get_model("core", "Room")


def _snapshot(room_model):
    return {
        (r.room_code, r.section): (
            r.capacity,
            r.exam_capacity,
            r.building,
            r.wing,
            r.department,
            r.room_type,
            r.floor,
        )
        for r in room_model.objects.all()
    }


def _seed(room_model):
    def add(code, section, capacity, building, wing, department, floor=None):
        room_model.objects.create(
            room_code=code,
            section=section,
            capacity=capacity,
            exam_capacity=capacity,
            building=building,
            wing=wing,
            department=department,
            floor=floor,
        )

    add("239FC007", "F", 70, "TV", "F04", "CS,CS2")  # a CS sheet room
    add(
        "239GC002", "F", 56, "239", "G12", "IS,IS2", 0
    )  # an IS sheet room, building recorded as 239
    add("227FC004", "F", 44, "227", "208", "CYP,CYP2")  # a CYP sheet room
    add("239FC007", "M", 50, "TV", "F04", "CS,CS2")  # same code on the men's campus: not touched
    add("204SC006", "M", 33, "204", "326", "IS,IS2")  # a sheet code taken on the men's campus
    add("204X999", "F", 38, "204", "360", "AI", 3)  # hall 360 in 204 already has a room
    add("172FA001", "M", 45, "", "", "CS,CS2")  # a room the sheet does not mention


@pytest.mark.django_db(transaction=True)
def test_the_sheet_updates_inserts_and_skips_exactly_as_decided():
    room = _migrate(BEFORE)
    _seed(room)
    room = _migrate(AFTER)
    rows = _snapshot(room)

    # exam_capacity from the sheet on the women's campus; capacity and department untouched.
    assert rows[("239FC007", "F")] == (70, 56, "TV", "F04", "CS,CS2", "lecture", None)
    assert rows[("227FC004", "F")] == (44, 44, "227", "208", "CYP,CYP2", "lecture", None)
    # the sheet wins on building: 239 -> TV; floor and department kept.
    assert rows[("239GC002", "F")] == (56, 28, "TV", "G12", "IS,IS2", "lecture", 0)
    # the men's campus and rooms the sheet does not name are left exactly as they were.
    assert rows[("239FC007", "M")] == (50, 50, "TV", "F04", "CS,CS2", "lecture", None)
    assert rows[("172FA001", "M")] == (45, 45, "", "", "CS,CS2", "lecture", None)
    assert rows[("204SC006", "M")] == (33, 33, "204", "326", "IS,IS2", "lecture", None)
    assert rows[("204X999", "F")] == (38, 38, "204", "360", "AI", "lecture", 3)
    # inserted: codes free on both campuses and halls not yet held; capacity = the sheet's actual.
    assert rows[("239FC005", "F")] == (54, 20, "TV", "F07", "CS,CS2", "lecture", None)
    assert rows[("204GC007", "F")] == (40, 45, "204", "121 B", "CS,CS2", "lecture", None)
    # skipped: 204SC006's code exists on the men's campus; hall 360 in 204 already has a room.
    assert ("204SC006", "F") not in rows
    assert ("204SC003", "F") not in rows
    assert len(rows) == 9

    # Running the data step again changes nothing.
    again = _snapshot(room)
    from importlib import import_module

    step = import_module("core.migrations.0073_rooms_1448t2_women_campus")

    class _Editor:
        connection = connection

    from django.apps import apps as live_apps

    step.apply_sheet(live_apps, _Editor())
    assert _snapshot(room) == again

    # Reversing restores exactly the state before.
    room = _migrate(BEFORE)
    reverted = _snapshot(room)
    assert reverted[("239GC002", "F")][2] == "239"
    assert reverted[("239FC007", "F")][1] == 70
    assert ("239FC005", "F") not in reverted and ("204GC007", "F") not in reverted
    assert len(reverted) == 7

    _migrate(MigrationExecutor(connection).loader.graph.leaf_nodes())


@pytest.mark.django_db(transaction=True)
def test_an_empty_rooms_table_is_left_empty():
    room = _migrate(BEFORE)
    assert not room.objects.exists()
    room = _migrate(AFTER)
    assert not room.objects.exists()
    _migrate(MigrationExecutor(connection).loader.graph.leaf_nodes())
